"""web/routes.py — 页面路由。"""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

from app.services.monitor_service import load_monitor_payload


bp = Blueprint("web", __name__)


@bp.get("/")
def index():
    return render_template("monitor.html")


@bp.get("/keyword-manage")
def keyword_manage():
    return redirect(url_for("web.index", view="keyword-manage"))


@bp.get("/article-hit-detail")
def article_hit_detail():
    return render_template("article_hit_detail.html")


@bp.get("/article-hit-detail-demo")
def article_hit_detail_demo():
    return redirect("/article-hit-detail?article_id=xhs_work_demo")


@bp.get("/keyword-turnover")
def keyword_turnover():
    return render_template("keyword_turnover.html")
