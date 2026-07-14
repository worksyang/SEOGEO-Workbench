from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

from app.services.account_score_analysis_service import build_account_score_analysis_context
from app.services.account_score_formula_service import build_account_score_formula_context
from app.services.monitor_service import load_monitor_payload


bp = Blueprint("web", __name__)


@bp.get("/")
def index():
    return render_template("monitor.html")


@bp.get("/keyword-manage")
def keyword_manage():
    return redirect(url_for("web.index", view="keyword-manage"))


@bp.get("/keyword-turnover")
def keyword_turnover():
    return render_template("keyword_turnover.html")


@bp.get("/article-hit-detail")
def article_hit_detail():
    return render_template("article_hit_detail.html")


@bp.get("/article-hit-detail-demo")
def article_hit_detail_demo():
    return redirect("/article-hit-detail?article_id=art_749d447ea394")


@bp.get("/account-score-analysis")
def account_score_analysis():
    payload = load_monitor_payload()
    context = build_account_score_analysis_context(payload)
    return render_template("account_score_analysis.html", **context)


@bp.get("/account-score-formula")
def account_score_formula():
    context = build_account_score_formula_context()
    return render_template("account_score_formula.html", **context)
