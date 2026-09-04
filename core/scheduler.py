"""Background scheduler: two jobs, both running in-process (see
start_scheduler(), called from core/apps.py CoreConfig.ready()) so the
Explore page's Start/Stop toggle (core/views.py scheduler_toggle_view)
can control them without shelling out to an OS scheduler — there's no
cron on Windows, and controlling Task Scheduler from a web view is
fragile. The scheduler always ticks once it's started; each job function
itself checks its own core.models.SchedulerJobState.is_enabled and
returns immediately when off, which is what actually makes Start/Stop
work regardless of how many worker processes serve the app.

Job 1 — check_dispute_timeouts(): every minute, queries the switch DB
(core.switch_db) for transactions with Overall Status = "TIMEOUT" that
appeared since the last check, and emails a summary (grouped by
Aggregator / Payment Processor) if any are found.

Job 2 — send_daily_report(): every day at 09:00, emails the previous
day's Issuer-wise / Acquirer-wise / Aggregator-wise transaction report —
the same numbers and workbook shape as the Dashboard's "General" tab
export (core.general_report.compute_general_report +
core.views._build_general_report_workbook)."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from email.mime.image import MIMEImage
from io import BytesIO

from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from . import switch_db
from .services import MAIL_SIGNATURE_IMAGE_CID, build_signature_blocks, resolve_mail_signature

logger = logging.getLogger(__name__)

_scheduler = None


def _attach_signature_image(email: EmailMultiAlternatives) -> None:
    """Same inline sct-signature banner every other outgoing email in
    this app attaches (see core.views.verification_send_mail_view) —
    referenced by build_signature_blocks()'s HTML as
    cid:MAIL_SIGNATURE_IMAGE_CID."""
    signature_path = finders.find("core/img/mail-signature.png")
    if not signature_path:
        return
    email.mixed_subtype = "related"
    with open(signature_path, "rb") as f:
        image = MIMEImage(f.read())
    image.add_header("Content-ID", f"<{MAIL_SIGNATURE_IMAGE_CID}>")
    image.add_header("Content-Disposition", "inline", filename="mail-signature.png")
    email.attach(image)


def _recipients(report_type: str) -> tuple[list[str], list[str]]:
    from .models import ScheduledReportRecipient

    to_all, cc_all = [], []
    for r in ScheduledReportRecipient.objects.filter(report_type=report_type, is_active=True):
        to_all += [a.strip() for a in r.to_emails.split(",") if a.strip()]
        cc_all += [a.strip() for a in r.cc_emails.split(",") if a.strip()]
    # dict.fromkeys de-dupes while keeping first-seen order — plain set()
    # wouldn't give a stable To: header order across runs.
    return list(dict.fromkeys(to_all)), list(dict.fromkeys(cc_all))


def _get_job_state(job_key: str):
    from .models import SchedulerJobState

    state, _ = SchedulerJobState.objects.get_or_create(job_key=job_key)
    return state


def _naive_local(dt):
    """switch_db's own query compares against transaction_entry.created,
    which is stored as a naive timestamp holding Asia/Kathmandu wall-clock
    time (see switch_db.py's docstring) — every caller into it, including
    switch_db.date_window() itself, strips tzinfo the same way."""
    return timezone.localtime(dt).replace(tzinfo=None)


# How far back each check re-scans on top of "since the last check" — a
# transaction's row in transaction_entry can exist before
# transaction_payment_status is written, so a transaction created just
# before a check ran may not show "TIMEOUT" yet on that check but does on
# the next one. Widening the window re-sees it; the Network Reference Id
# dedup below (not the window) is what stops it being emailed twice.
_DISPUTE_LOOKBACK_BUFFER_MINUTES = 5


def check_dispute_timeouts() -> None:
    from .models import AlertedTimeoutTransaction, ScheduledReportRecipient, SchedulerJobState

    state = _get_job_state(SchedulerJobState.JOB_DISPUTE_ALERT)
    if not state.is_enabled:
        return

    now = timezone.now()
    since = state.last_checked_at or (now - timedelta(minutes=1))
    lookback_since = since - timedelta(minutes=_DISPUTE_LOOKBACK_BUFFER_MINUTES)
    naive_since, naive_now = _naive_local(lookback_since), _naive_local(now)

    try:
        rows = switch_db.fetch_transactions(naive_since, naive_now)
    except switch_db.SwitchDBError as exc:
        state.last_run_at = now
        state.last_result = f"Error: {exc}"
        state.save(update_fields=["last_run_at", "last_result"])
        logger.warning("Dispute/timeout check failed: %s", exc)
        return

    timeouts = [r for r in rows if str(r.get("Overall Status") or "").strip().upper() == "TIMEOUT"]
    state.last_checked_at = now
    state.last_run_at = now

    if not timeouts:
        state.last_result = f"No new timeouts ({naive_since:%Y-%m-%d %H:%M} – {naive_now:%H:%M})"
        state.save(update_fields=["last_checked_at", "last_run_at", "last_result"])
        return

    ref_ids = [r.get("Network Reference Id") for r in timeouts if r.get("Network Reference Id")]
    already_alerted = set(
        AlertedTimeoutTransaction.objects.filter(network_reference_id__in=ref_ids).values_list(
            "network_reference_id", flat=True
        )
    )
    new_timeouts = [r for r in timeouts if r.get("Network Reference Id") not in already_alerted]

    if not new_timeouts:
        state.last_result = f"{len(timeouts)} timeout(s) seen, already alerted"
        state.save(update_fields=["last_checked_at", "last_run_at", "last_result"])
        return

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in new_timeouts:
        key = (r.get("Aggregator") or "—", r.get("Payment Processor") or "—")
        groups[key].append(r.get("Network Reference Id") or "—")

    to_list, cc_list = _recipients(ScheduledReportRecipient.REPORT_DISPUTE_ALERT)
    if not to_list:
        state.last_result = f"{len(new_timeouts)} new timeout(s) found but no recipients configured"
        state.save(update_fields=["last_checked_at", "last_run_at", "last_result"])
        logger.warning("Dispute/timeout alert: %d new timeout(s) found but no recipients configured", len(new_timeouts))
        return

    rows_html = "".join(
        f"<tr><td style='border:1px solid #999;padding:4px 8px;'>{agg}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;'>{proc}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{len(ref_ids_)}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;'>{', '.join(ref_ids_)}</td></tr>"
        for (agg, proc), ref_ids_ in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    )
    table_html = (
        "<table style='border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:12.5px;'>"
        "<thead><tr>"
        "<th style='border:1px solid #999;padding:4px 8px;background:#f2f2f2;'>Aggregator</th>"
        "<th style='border:1px solid #999;padding:4px 8px;background:#f2f2f2;'>Payment Processor</th>"
        "<th style='border:1px solid #999;padding:4px 8px;background:#f2f2f2;'>Count</th>"
        "<th style='border:1px solid #999;padding:4px 8px;background:#f2f2f2;'>Network Reference Id(s)</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
    )

    signature = resolve_mail_signature("", user=None)
    sig_text, sig_html = build_signature_blocks(signature)
    subject = f"Timeout Alert — {len(new_timeouts)} transaction(s) — {naive_now:%Y-%m-%d %H:%M}"
    html_body = (
        f"<p>{len(new_timeouts)} transaction(s) timed out as of "
        f"<strong>{naive_now:%Y-%m-%d %H:%M}</strong>.</p>"
        f"{table_html}<p>{sig_html}</p>"
    )
    text_body = (
        f"{len(new_timeouts)} transaction(s) timed out as of {naive_now:%Y-%m-%d %H:%M}.\n\n"
        + "\n".join(f"{agg} / {proc}: {len(ref_ids_)} — {', '.join(ref_ids_)}" for (agg, proc), ref_ids_ in groups.items())
        + f"\n\n{sig_text}"
    )

    email = EmailMultiAlternatives(subject=subject, body=text_body, to=to_list, cc=cc_list or None)
    email.attach_alternative(html_body, "text/html")
    _attach_signature_image(email)
    try:
        email.send(fail_silently=False)
        state.last_result = f"Sent: {len(new_timeouts)} new timeout(s) to {len(to_list)} recipient(s)"
        # Only recorded once the email actually went out — a send failure
        # leaves these ref ids un-alerted so the next check retries them,
        # rather than silently swallowing them into the dedup ledger.
        new_ref_ids = [r.get("Network Reference Id") for r in new_timeouts if r.get("Network Reference Id")]
        AlertedTimeoutTransaction.objects.bulk_create(
            [AlertedTimeoutTransaction(network_reference_id=rid) for rid in dict.fromkeys(new_ref_ids)],
            ignore_conflicts=True,
        )
    except Exception as exc:  # noqa: BLE001 - a bad SMTP config shouldn't kill the scheduler thread
        state.last_result = f"Email send failed: {exc}"
        logger.exception("Dispute/timeout alert email failed")
    state.save(update_fields=["last_checked_at", "last_run_at", "last_result"])


def _split_rows_html_table(name_header: str, rows: list[dict]) -> str:
    """Aggregator-wise / Issuer-wise / Acquirer-wise table — same columns
    as their sheet in the attached workbook (see
    core.views._populate_named_split_sheet), just rendered inline so the
    breakdown is visible in the email itself, not only in the
    attachment."""
    header_html = "".join(
        f"<th style='border:1px solid #999;padding:4px 8px;background:#f2f2f2;'>{h}</th>"
        for h in [name_header, "Success", "Success amt (Rs.)", "Failed", "Failed amt (Rs.)", "Total", "Total amt (Rs.)"]
    )
    body_html = "".join(
        "<tr>"
        f"<td style='border:1px solid #999;padding:4px 8px;'>{r['name']}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['success_count']}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['success_amount']:,.2f}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['failed_count']}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['failed_amount']:,.2f}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['total_count']}</td>"
        f"<td style='border:1px solid #999;padding:4px 8px;text-align:right;'>{r['total_amount']:,.2f}</td>"
        "</tr>"
        for r in rows
    )
    return (
        "<table style='border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:12px;margin-top:6px;'>"
        f"<thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"
    )


def send_daily_report() -> None:
    from .general_report import compute_general_report
    from .models import ScheduledReportRecipient, SchedulerJobState

    state = _get_job_state(SchedulerJobState.JOB_DAILY_REPORT)
    if not state.is_enabled:
        return

    now = timezone.now()
    yesterday = timezone.localdate(now) - timedelta(days=1)
    start, end = switch_db.date_window(yesterday, yesterday)

    try:
        rows = switch_db.fetch_transactions(start, end)
    except switch_db.SwitchDBError as exc:
        state.last_run_at = now
        state.last_result = f"Error: {exc}"
        state.save(update_fields=["last_run_at", "last_result"])
        logger.warning("Daily report failed: %s", exc)
        return

    report = compute_general_report(rows)
    to_list, cc_list = _recipients(ScheduledReportRecipient.REPORT_DAILY_REPORT)
    state.last_run_at = now

    if not to_list:
        state.last_result = f"Report built ({report['totals']['total_count']} txns) but no recipients configured"
        state.save(update_fields=["last_run_at", "last_result"])
        logger.warning("Daily report: no recipients configured")
        return

    # Lazy import: core.views pulls in every view-only dependency (auth
    # decorators, request/response helpers, etc.) that this module has no
    # other reason to load until a report is actually about to be sent.
    from .views import _build_general_report_workbook

    period = f"{yesterday:%d %b %Y}"
    wb = _build_general_report_workbook(report, period)
    buf = BytesIO()
    wb.save(buf)

    totals = report["totals"]
    signature = resolve_mail_signature("", user=None)
    sig_text, sig_html = build_signature_blocks(signature)
    subject = f"Daily Transaction Report — {yesterday:%Y-%m-%d}"
    html_body = (
        f"<p>Transaction report for <strong>{yesterday:%Y-%m-%d}</strong>.</p>"
        "<table style='border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:13px;'>"
        f"<tr><td style='padding:2px 8px 2px 0;color:#555;'>Total</td><td>{totals['total_count']}</td></tr>"
        f"<tr><td style='padding:2px 8px 2px 0;color:#555;'>Success</td>"
        f"<td>{totals['success_count']} (Rs. {totals['success_amount']:,.2f})</td></tr>"
        f"<tr><td style='padding:2px 8px 2px 0;color:#555;'>Failed</td>"
        f"<td>{totals['failed_count']} (Rs. {totals['failed_amount']:,.2f})</td></tr>"
        "</table>"
        "<p style='margin-top:16px;'><strong>Aggregator-wise</strong></p>"
        f"{_split_rows_html_table('Aggregator', report['aggregator_rows'])}"
        "<p style='margin-top:16px;'><strong>Issuer-wise (Debtor bank)</strong></p>"
        f"{_split_rows_html_table('Bank', report['issuer_rows'])}"
        "<p style='margin-top:16px;'><strong>Acquirer-wise (Creditor bank)</strong></p>"
        f"{_split_rows_html_table('Bank', report['acquirer_rows'])}"
        "<p style='margin-top:16px;'>Member-wise detail is in the attached workbook.</p>"
        f"<p>{sig_html}</p>"
    )
    def _text_section(title, rows):
        lines = [f"{r['name']}: success {r['success_count']}, failed {r['failed_count']}, total {r['total_count']}" for r in rows]
        return f"\n{title}\n" + "\n".join(lines) if lines else f"\n{title}\n(none)"

    text_body = (
        f"Transaction report for {yesterday:%Y-%m-%d}.\n"
        f"Total: {totals['total_count']}\n"
        f"Success: {totals['success_count']} (Rs. {totals['success_amount']:.2f})\n"
        f"Failed: {totals['failed_count']} (Rs. {totals['failed_amount']:.2f})\n"
        + _text_section("Aggregator-wise", report["aggregator_rows"])
        + _text_section("Issuer-wise (Debtor bank)", report["issuer_rows"])
        + _text_section("Acquirer-wise (Creditor bank)", report["acquirer_rows"])
        + f"\n\n{sig_text}"
    )

    email = EmailMultiAlternatives(subject=subject, body=text_body, to=to_list, cc=cc_list or None)
    email.attach_alternative(html_body, "text/html")
    _attach_signature_image(email)
    email.attach(
        f"daily_report_{yesterday:%Y-%m-%d}.xlsx",
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    try:
        email.send(fail_silently=False)
        state.last_result = f"Sent to {len(to_list)} recipient(s)"
    except Exception as exc:  # noqa: BLE001 - a bad SMTP config shouldn't kill the scheduler thread
        state.last_result = f"Email send failed: {exc}"
        logger.exception("Daily report email failed")
    state.save(update_fields=["last_run_at", "last_result"])


def start_scheduler() -> None:
    """Idempotent — safe to call more than once (only the first call
    actually starts anything). See core/apps.py for the guard against
    starting this twice under the runserver autoreloader, or at all
    during a one-shot management command like migrate/makemigrations."""
    global _scheduler
    if _scheduler is not None:
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from django.conf import settings

    scheduler = BackgroundScheduler(timezone=timezone.get_current_timezone())
    scheduler.add_job(
        check_dispute_timeouts, "interval", minutes=1,
        id="dispute_alert_check", max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    scheduler.add_job(
        send_daily_report, "cron", hour=9, minute=0,
        id="daily_report_job", max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Background scheduler started (dispute check every 1 min, daily report at 09:00 %s)", settings.TIME_ZONE)
