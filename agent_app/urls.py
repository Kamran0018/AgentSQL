"""
Data-Agent AI — App URL Configuration
"""

from django.urls import path
from . import views

urlpatterns = [
    path("",                   views.home,                name="home"),
    path("healthz/",           views.healthz_view,        name="healthz"),
    path("signup/",            views.signup_view,         name="signup"),
    path("login/",             views.login_view,          name="login"),
    path("logout/",            views.logout_view,         name="logout"),
    path("api/explain/",       views.explain_view,        name="explain"),
    path("api/clear-history/", views.clear_history_view,  name="clear_history"),
    # Source management APIs
    path("api/sources/upload/",  views.upload_source_view,  name="source_upload"),
    path("api/sources/connect/", views.connect_source_view, name="source_connect"),
    path("api/sources/select/",  views.select_source_view,  name="source_select"),
    path("api/sources/delete/",  views.delete_source_view,  name="source_delete"),
]
