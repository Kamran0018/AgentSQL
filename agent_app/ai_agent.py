"""
Data-Agent AI — Multi-Agent LangGraph Workflow
================================================
Pipeline with intent classification, conversation memory, auto-visualization, and SQL explanation:

    START → intent_router ─┬─ (greeting / clarification / out_of_scope) ───────────────► END
                           │
                           └─ (query) → sql_writer → sql_executor ─┬─ success → visualizer → END
                                             ▲                      │
                                             └── retry (max 3) ─────┘
                                                                    └─ end (give up) → END
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, START, StateGraph

from .db_utils import (
    demo_db_url,
    dialect_of,
    execute_sql,
    get_db_schema,
)
from .prompts import (
    DIALECT_HINTS,
    DIALECT_NAMES,
    EXPLAIN_PROMPT,
    INTENT_ROUTER_PROMPT,
    SQL_WRITER_PROMPT,
    VISUALIZER_PROMPT,
)

# Load environment
load_dotenv(Path(__file__).resolve().parent / ".env")


class LazyLLM:
    """
    Lazy wrapper around ChatGroq so Django checks, migrations,
    and tests don't fail when GROQ_API_KEY is not yet configured.
    """
    def __init__(self):
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            from langchain_groq import ChatGroq
            api_key = os.getenv("GROQ_API_KEY", "")
            model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
            self._llm = ChatGroq(
                groq_api_key=api_key or "placeholder-key",
                model=model_name,
                temperature=0,
            )
        return self._llm

    def invoke(self, *args, **kwargs):
        return self._get_llm().invoke(*args, **kwargs)


# Module-level llm instance (can be patched in tests: patch("agent_app.ai_agent.llm", fake_llm))
llm = LazyLLM()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LangGraph State Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AgentState(TypedDict):
    question: str                # User's natural-language question
    db_url: str                  # Database connection URL
    intent: str                  # "query", "greeting", "clarification", "out_of_scope"
    message: str                 # Friendly message for non-query intents
    sql_query: str               # Generated SQL query
    error: str                   # Error message from failed execution
    data_result: Dict[str, Any]  # Successful query results
    iteration_count: int         # Current retry attempt number
    history: List[Dict]          # Conversation history [{question, sql}, ...]
    chart_config: Dict[str, Any] # Chart.js configuration (empty = no chart)


def _format_history(history: List[Dict]) -> str:
    if history:
        history_lines = []
        for i, turn in enumerate(history, 1):
            history_lines.append(f"  Turn {i}: Q: {turn['question']}")
            if turn.get("sql"):
                history_lines.append(f"          SQL: {turn['sql']}")
        return "\n".join(history_lines)
    return "No previous conversation."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node 0 — Intent Router (Classifies input before SQL)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def intent_router_node(state: AgentState) -> dict:
    """
    Classify user input into greeting, clarification, out_of_scope, or query.
    Takes into account the schema and conversation context.
    """
    db_url = state.get("db_url") or demo_db_url()
    schema = get_db_schema(db_url)
    history_str = _format_history(state.get("history", []))

    prompt = PromptTemplate.from_template(INTENT_ROUTER_PROMPT)
    formatted = prompt.format(
        schema=schema,
        history=history_str,
        question=state["question"],
    )

    try:
        response = llm.invoke(formatted)
        content = getattr(response, "content", str(response)).strip()
        # Robustly extract JSON object using regex
        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            parsed = json.loads(json_match.group(0))
        else:
            clean_content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
            clean_content = re.sub(r"\s*```$", "", clean_content, flags=re.MULTILINE).strip()
            parsed = json.loads(clean_content)

        intent = parsed.get("intent", "query").lower().strip()
        message = parsed.get("message", "").strip()

        if intent not in ("greeting", "clarification", "out_of_scope", "query"):
            intent = "query"

        # Provide sensible default messages if model left message blank
        if intent == "greeting" and not message:
            message = "Hello! I am AgentSQL, your autonomous data analyst. How can I assist you with your database today?"
        elif intent == "clarification" and not message:
            message = "Could you please clarify your question? For example, specify what information or metric you want to see from the database."
        elif intent == "out_of_scope" and not message:
            message = "I am a dedicated database query assistant. Please ask a question related to your database data."

        return {
            "intent": intent,
            "message": message,
        }

    except Exception:
        # Fallback heuristic if intent parsing fails
        q_lower = state["question"].lower().strip()
        greeting_words = {"hi", "hello", "hey", "hola", "namaste", "good morning", "good evening", "good afternoon"}
        if q_lower in greeting_words or (len(q_lower.split()) <= 2 and any(q_lower.startswith(w) for w in ("hi", "hello", "hey"))):
            return {
                "intent": "greeting",
                "message": "Hello! I am AgentSQL, your autonomous data analyst. How can I assist you with your database today?",
            }
        # Default to query if classification fails so standard flow continues
        return {
            "intent": "query",
            "message": "",
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node 1 — SQL Writer (LLM generates the query, with memory)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sql_writer_node(state: AgentState) -> dict:
    """Ask the LLM to generate (or fix) a SQL query, using schema and memory."""
    db_url = state.get("db_url") or demo_db_url()
    dialect = dialect_of(db_url)
    dialect_name = DIALECT_NAMES.get(dialect, "SQL")
    dialect_hints = DIALECT_HINTS.get(dialect, "")

    schema = get_db_schema(db_url)
    history_str = _format_history(state.get("history", []))

    prompt = PromptTemplate.from_template(SQL_WRITER_PROMPT)
    formatted = prompt.format(
        dialect_name=dialect_name,
        dialect_hints=dialect_hints,
        schema=schema,
        question=state["question"],
        error=state.get("error", ""),
        history=history_str,
    )

    response = llm.invoke(formatted)
    content = getattr(response, "content", str(response))

    clean_sql = (
        content
        .replace("```sql", "")
        .replace("```", "")
        .strip()
    )

    return {
        "sql_query": clean_sql,
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node 2 — SQL Executor (runs query with read-only guards)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sql_executor_node(state: AgentState) -> dict:
    """Execute the SQL query and return data or an error."""
    # Safety guard: Never execute SQL if intent is not query or query is empty
    if state.get("intent") != "query" or not state.get("sql_query"):
        return {
            "error": "Non-query intent cannot execute SQL.",
        }

    db_url = state.get("db_url") or demo_db_url()
    result = execute_sql(db_url, state["sql_query"])

    if result.get("success"):
        return {
            "data_result": result,
            "error": "",
        }
    else:
        return {
            "error": result.get("error", "Unknown database error occurred"),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node 3 — Visualizer (generates Chart.js config)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def visualizer_node(state: AgentState) -> dict:
    """Decide best chart type and return Chart.js configuration."""
    data_res = state.get("data_result", {})
    columns = data_res.get("columns", [])
    data = data_res.get("data", [])

    if not data or len(data) == 0 or len(columns) < 2:
        return {"chart_config": {}}

    prompt = PromptTemplate.from_template(VISUALIZER_PROMPT)
    formatted = prompt.format(
        columns=json.dumps(columns),
        sample_data=json.dumps(data[:10]),
    )

    try:
        response = llm.invoke(formatted)
        content = getattr(response, "content", str(response))
        clean_json = (
            content
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )
        config = json.loads(clean_json)

        if config.get("chart_type") == "none":
            return {"chart_config": {}}

        return {"chart_config": config}
    except Exception:
        return {"chart_config": {}}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conditional Routing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def route_after_intent(state: AgentState) -> str:
    """Route to sql_writer only if intent is 'query'; otherwise terminate immediately."""
    if state.get("intent") == "query":
        return "sql_writer"
    return "end"


def should_retry(state: AgentState) -> str:
    """Determine next node based on execution outcome and retry count."""
    if not state.get("error"):
        return "visualizer"

    if state.get("iteration_count", 0) < 3:
        return "sql_writer"

    return "end"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Build the LangGraph Workflow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

workflow = StateGraph(AgentState)

workflow.add_node("intent_router", intent_router_node)
workflow.add_node("sql_writer", sql_writer_node)
workflow.add_node("sql_executor", sql_executor_node)
workflow.add_node("visualizer", visualizer_node)

workflow.add_edge(START, "intent_router")

workflow.add_conditional_edges(
    "intent_router",
    route_after_intent,
    {
        "sql_writer": "sql_writer",
        "end": END,
    },
)

workflow.add_edge("sql_writer", "sql_executor")

workflow.add_conditional_edges(
    "sql_executor",
    should_retry,
    {
        "visualizer": "visualizer",
        "sql_writer": "sql_writer",
        "end": END,
    },
)

workflow.add_edge("visualizer", END)

app = workflow.compile()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public Interface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_analytics_agent(
    question: str,
    db_url: Optional[str] = None,
    history: Optional[List[Dict]] = None
) -> dict:
    """
    Run the multi-agent SQL pipeline for a user question.
    """
    if not db_url:
        db_url = demo_db_url()

    initial_state: AgentState = {
        "question": question,
        "db_url": db_url,
        "intent": "query",
        "message": "",
        "sql_query": "",
        "error": "",
        "data_result": {},
        "iteration_count": 0,
        "history": history or [],
        "chart_config": {},
    }

    final_state = app.invoke(initial_state)
    return final_state


def explain_sql(sql: str) -> str:
    """Generate a friendly, natural-language explanation of a SQL query."""
    prompt = PromptTemplate.from_template(EXPLAIN_PROMPT)
    formatted = prompt.format(sql=sql)

    try:
        response = llm.invoke(formatted)
        return getattr(response, "content", str(response)).strip()
    except Exception as e:
        return f"Could not explain query: {str(e)}"
