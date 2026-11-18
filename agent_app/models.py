"""
Data-Agent AI — Models
======================
"""

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from .crypto import decrypt, encrypt


class DataSource(models.Model):
    KIND_CHOICES = [
        ("file", "File Upload (SQLite)"),
        ("remote", "Remote Database (PostgreSQL / MySQL)"),
    ]

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="data_sources")
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    dialect = models.CharField(max_length=50)  # sqlite, postgresql, mysql
    file_path = models.CharField(max_length=500, blank=True, default="")
    encrypted_url = models.TextField(blank=True, default="")
    display_info = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def set_url(self, raw_url: str):
        self.encrypted_url = encrypt(raw_url)

    def get_url(self) -> str:
        return decrypt(self.encrypted_url)

    def __str__(self):
        return f"{self.name} ({self.kind} - {self.owner.username})"


class ChatTurn(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="chat_turns")
    # source can be null for demo db
    source = models.ForeignKey(DataSource, on_delete=models.CASCADE, null=True, blank=True, related_name="chat_turns")
    question = models.TextField()
    sql = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"ChatTurn {self.id} for {self.user.username}"


class UsageDay(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="usage_days")
    date = models.DateField(default=timezone.now)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("user", "date")

    def __str__(self):
        return f"{self.user.username} on {self.date}: {self.count} queries"
