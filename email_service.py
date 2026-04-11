"""
A2Z Dialer Email Service
Uses Resend API (or falls back to logging if RESEND_API_KEY not set).
"""
import logging
import os
from typing import Optional

logger = logging.getLogger("a2z.email")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@a2zdialer.com")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@a2zdialer.com")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


def _html_email(title: str, body_html: str, cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> str:
    cta_block = ""
    if cta_label and cta_url:
        cta_block = f"""
        <div style="text-align:center;margin:32px 0;">
          <a href="{cta_url}" style="background:#0284c7;color:#fff;padding:12px 28px;border-radius:10px;font-weight:600;font-size:14px;text-decoration:none;display:inline-block;">{cta_label}</a>
        </div>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>{title}</title></head>
    <body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
        <div style="background:linear-gradient(135deg,#0a0f1e 0%,#0d1526 100%);padding:28px 32px;">
          <h1 style="color:#fff;margin:0;font-size:22px;font-weight:700;">A2Z Dialer</h1>
          <p style="color:rgba(255,255,255,0.55);margin:4px 0 0;font-size:13px;">Real estate AI platform</p>
        </div>
        <div style="padding:32px;">
          <h2 style="color:#0f172a;margin:0 0 16px;font-size:20px;font-weight:700;">{title}</h2>
          {body_html}
          {cta_block}
        </div>
        <div style="border-top:1px solid #e2e8f0;padding:20px 32px;background:#f8fafc;">
          <p style="color:#94a3b8;font-size:12px;margin:0;">
            © 2026 A2Z Dialer · 123 AI Drive, Suite 100, New York, NY 10001<br>
            <a href="{FRONTEND_URL}/legal/privacy" style="color:#94a3b8;">Privacy Policy</a> ·
            <a href="{FRONTEND_URL}/legal/terms" style="color:#94a3b8;">Terms of Service</a>
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def _send(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.info(f"[EMAIL SKIP] Would send to {to}: {subject}")
        return True
    try:
        import resend  # type: ignore
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to,
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        logger.error(f"Email send failed to {to}: {e}")
        return False


def send_welcome(user_email: str, user_name: str, workspace_name: str) -> bool:
    html = _html_email(
        f"Welcome to A2Z Dialer, {user_name}!",
        f"""
        <p style="color:#475569;line-height:1.6;">Your workspace <strong>{workspace_name}</strong> is ready. Start deploying AI campaigns, browsing the marketplace, and building your territory presence.</p>
        <p style="color:#475569;line-height:1.6;">Questions? Reply to this email or chat with us at <a href="mailto:{SUPPORT_EMAIL}" style="color:#0284c7;">{SUPPORT_EMAIL}</a>.</p>
        """,
        cta_label="Go to Dashboard",
        cta_url=f"{FRONTEND_URL}/app",
    )
    return _send(user_email, f"Welcome to A2Z Dialer, {user_name}!", html)


def send_appointment_confirmation(homeowner_email: str, homeowner_name: str, agent_name: str, appointment_time: str, property_address: str) -> bool:
    html = _html_email(
        "Your appointment is confirmed",
        f"""
        <p style="color:#475569;line-height:1.6;">Hi {homeowner_name},</p>
        <p style="color:#475569;line-height:1.6;">Your appointment with <strong>{agent_name}</strong> has been confirmed for <strong>{appointment_time}</strong>.</p>
        <p style="color:#475569;line-height:1.6;">Property: {property_address}</p>
        <p style="color:#475569;line-height:1.6;">If you need to reschedule, please contact your agent directly.</p>
        """,
    )
    return _send(homeowner_email, "Your appointment is confirmed — A2Z Dialer", html)


def send_marketplace_purchase_confirmation(buyer_email: str, listing_type: str, territory_name: str, amount: float, purchase_id: int) -> bool:
    html = _html_email(
        "Purchase Confirmed",
        f"""
        <p style="color:#475569;line-height:1.6;">Your marketplace purchase has been processed successfully.</p>
        <div style="background:#f8fafc;border-radius:12px;padding:20px;margin:16px 0;">
          <p style="margin:4px 0;color:#0f172a;"><strong>Type:</strong> {listing_type.replace('_', ' ').title()}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Territory:</strong> {territory_name}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Amount:</strong> ${amount:.2f}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Purchase ID:</strong> #{purchase_id}</p>
        </div>
        <p style="color:#475569;font-size:13px;">View full seller details in your purchases dashboard.</p>
        """,
        cta_label="View Purchase",
        cta_url=f"{FRONTEND_URL}/app/marketplace/purchases",
    )
    return _send(buyer_email, "Marketplace purchase confirmed — A2Z Dialer", html)


def send_password_reset(email: str, reset_token: str) -> bool:
    reset_url = f"{FRONTEND_URL}/reset-password?token={reset_token}"
    html = _html_email(
        "Reset your password",
        f"""
        <p style="color:#475569;line-height:1.6;">We received a request to reset your password. Click the button below to set a new password. This link expires in 1 hour.</p>
        <p style="color:#475569;font-size:13px;">If you didn't request this, you can safely ignore this email.</p>
        """,
        cta_label="Reset Password",
        cta_url=reset_url,
    )
    return _send(email, "Reset your A2Z Dialer password", html)


def send_payment_failed(email: str, workspace_name: str) -> bool:
    html = _html_email(
        "Payment Failed",
        f"""
        <p style="color:#475569;line-height:1.6;">Hi, we were unable to process your payment for workspace <strong>{workspace_name}</strong>.</p>
        <p style="color:#475569;line-height:1.6;">Please update your payment method to continue using A2Z Dialer without interruption.</p>
        """,
        cta_label="Update Payment Method",
        cta_url=f"{FRONTEND_URL}/app/billing",
    )
    return _send(email, "Action required: Payment failed — A2Z Dialer", html)


def send_rank_change(email: str, agent_name: str, territory_name: str, old_rank: int, new_rank: int) -> bool:
    moved = "up" if new_rank < old_rank else "down"
    html = _html_email(
        f"Your rank changed in {territory_name}",
        f"""
        <p style="color:#475569;line-height:1.6;">Hi {agent_name},</p>
        <p style="color:#475569;line-height:1.6;">Your agent rank in <strong>{territory_name}</strong> has moved {moved} from <strong>#{old_rank}</strong> to <strong>#{new_rank}</strong>.</p>
        """,
        cta_label="View My Profile",
        cta_url=f"{FRONTEND_URL}/app/top-agent/profile",
    )
    return _send(email, f"Your rank changed in {territory_name} — A2Z Dialer", html)


def send_voice_clone_activated(
    email: str,
    agent_name: str,
    workspace_name: str,
    voice_name: str,
    campaigns_updated: int,
) -> bool:
    campaign_line = (
        f"We automatically assigned <strong>{voice_name}</strong> to "
        f"<strong>{campaigns_updated} campaign{'s' if campaigns_updated != 1 else ''}</strong> "
        f"in {workspace_name}. Every call those campaigns make will now sound exactly like you."
        if campaigns_updated > 0
        else f"Head to your campaigns and select <strong>\"{voice_name}\"</strong> to start using your cloned voice."
    )
    html = _html_email(
        "Your AI voice clone is live",
        f"""
        <p style="color:#475569;line-height:1.6;">Hi {agent_name},</p>
        <p style="color:#475569;line-height:1.6;">
            Your voice clone <strong>{voice_name}</strong> is ready and active in <strong>{workspace_name}</strong>.
        </p>
        <p style="color:#475569;line-height:1.6;">{campaign_line}</p>
        <p style="color:#475569;line-height:1.6;">
            Homeowners will hear your real voice on every AI call — building trust before you ever pick up the phone.
        </p>
        """,
        cta_label="Go to My Campaigns",
        cta_url=f"{FRONTEND_URL}/app/campaigns",
    )
    return _send(email, f"Your voice clone is live — A2Z Dialer", html)


def send_voice_clone_ready(email: str, agent_name: str, workspace_name: str) -> bool:
    html = _html_email(
        "Your AI voice clone is ready",
        f"""
        <p style="color:#475569;line-height:1.6;">Hi {agent_name},</p>
        <p style="color:#475569;line-height:1.6;">
            Your voice has been successfully cloned for <strong>{workspace_name}</strong>.
            Your AI campaigns will now sound exactly like you on every call.
        </p>
        <p style="color:#475569;line-height:1.6;">
            Head to your campaigns and select <strong>"My Voice"</strong> to start using your cloned voice.
        </p>
        """,
        cta_label="Go to My Campaigns",
        cta_url=f"{FRONTEND_URL}/app/campaigns",
    )
    return _send(email, "Your AI voice clone is ready — A2Z Dialer", html)


def send_appointment_reminder(homeowner_email: str, homeowner_name: str, agent_name: str, appointment_time: str, property_address: str) -> bool:
    subject = "Reminder: Your appointment tomorrow"
    html = _html_email(
        f"Hi {homeowner_name},",
        f"Just a quick reminder about your appointment with {agent_name} tomorrow regarding your property at {property_address}. Your scheduled time is {appointment_time}.",
        "Need to reschedule? Reply to this email.",
    )
    return _send(homeowner_email, subject, html)


def send_payment_receipt(email: str, workspace_name: str, amount: float, plan: str, invoice_url: str = "") -> bool:
    subject = f"Payment confirmed — {plan} plan"
    link_html = f'<a href="{invoice_url}">View invoice</a>' if invoice_url else ""
    html = _html_email(
        f"Payment confirmed for {workspace_name}",
        f"We received your payment of ${amount:.2f} for the {plan} plan. Thank you! {link_html}",
        "Manage your subscription at a2zdialer.com/app/billing",
    )
    return _send(email, subject, html)


def send_subscription_renewal_reminder(email: str, workspace_name: str, renewal_date: str, amount: float, plan: str) -> bool:
    subject = f"Your {plan} plan renews on {renewal_date}"
    html = _html_email(
        f"Upcoming renewal for {workspace_name}",
        f"Your {plan} plan will automatically renew on {renewal_date} for ${amount:.2f}. No action needed.",
        "To change or cancel your plan, visit a2zdialer.com/app/billing",
    )
    return _send(email, subject, html)


def send_weekly_digest(email: str, agent_name: str, workspace_name: str, calls_made: int, appointments_booked: int, connect_rate: float) -> bool:
    subject = f"Your weekly summary — {workspace_name}"
    html = _html_email(
        f"Weekly digest for {agent_name}",
        f"Here's what your AI did this week: {calls_made} calls made, {appointments_booked} appointments booked, {connect_rate:.1f}% connect rate.",
        "Keep dialing — your next listing is one call away.",
    )
    return _send(email, subject, html)


def send_royalty_payment(email: str, partner_name: str, amount: float, period: str) -> bool:
    subject = f"Royalty payment of ${amount:.2f} sent"
    html = _html_email(
        f"Royalty payment — {period}",
        f"Hi {partner_name}, your royalty payment of ${amount:.2f} for {period} has been processed. Funds will appear in your account within 3–5 business days.",
        "Questions? Contact us at support@a2zdialer.com",
    )
    return _send(email, subject, html)


def send_team_invite(invited_email: str, inviter_name: str, workspace_name: str, invite_token: str) -> bool:
    subject = f"{inviter_name} invited you to join {workspace_name} on A2Z Dialer"
    accept_url = f"https://a2zdialer.com/accept-invite?token={invite_token}"
    html = _html_email(
        f"You're invited to {workspace_name}",
        f"{inviter_name} has invited you to join their workspace on A2Z Dialer. Click below to accept.",
        f'<a href="{accept_url}" style="background:#2563eb;color:#fff;padding:12px 28px;border-radius:12px;text-decoration:none;font-weight:600;display:inline-block;">Accept Invitation</a>',
    )
    return _send(invited_email, subject, html)


def send_no_campaign_nudge(email: str, first_name: str) -> bool:
    """Day-3 nudge when no campaign has been created."""
    html = _html_email(
        f"Your AI dialer is waiting, {first_name}",
        """<p style="color:#475569;line-height:1.6;">You signed up 3 days ago but haven't launched a campaign yet. Here are 3 quick steps to your first call:</p>
        <ol style="color:#475569;line-height:2;">
          <li>Upload your expired listing CSV</li>
          <li>Choose a pathway script</li>
          <li>Hit launch — AI does the rest</li>
        </ol>""",
        cta_label="Upload Leads Now",
        cta_url=f"{FRONTEND_URL}/app/leads",
    )
    return _send(email, "Your AI dialer is waiting — 3 quick steps to your first call", html)


def send_no_calls_nudge(email: str, first_name: str) -> bool:
    """Day-7 push when no calls have been made."""
    html = _html_email(
        "You're leaving money on the table",
        f"""<p style="color:#475569;line-height:1.6;">Hi {first_name}, it's been a week and your AI dialer hasn't made a single call yet.</p>
        <p style="color:#475569;line-height:1.6;">Every day you wait is a day someone else is talking to your expired listings. Let the AI do the work.</p>""",
        cta_label="Launch Your First Campaign",
        cta_url=f"{FRONTEND_URL}/app/campaigns",
    )
    return _send(email, "You're leaving money on the table — A2Z Dialer", html)


def send_appointment_booked(email: str, agent_name: str, homeowner_name: str, property_address: str, appointment_time: str) -> bool:
    """Agent notification when an appointment is booked."""
    html = _html_email(
        f"Appointment booked — {homeowner_name}",
        f"""<p style="color:#475569;line-height:1.6;">Your AI just booked an appointment!</p>
        <div style="background:#f0fdf4;border-radius:12px;padding:20px;margin:16px 0;border-left:4px solid #22c55e;">
          <p style="margin:4px 0;color:#0f172a;"><strong>Homeowner:</strong> {homeowner_name}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Property:</strong> {property_address}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Time:</strong> {appointment_time}</p>
        </div>""",
        cta_label="View Appointment",
        cta_url=f"{FRONTEND_URL}/app/appointments",
    )
    return _send(email, f"🗓 Appointment booked — {homeowner_name}", html)


def send_lead_listed(email: str, agent_name: str, homeowner_name: str, readiness_score: int, price_dollars: float) -> bool:
    """Agent notification when a lead is listed on marketplace."""
    html = _html_email(
        f"Lead listed on marketplace",
        f"""<p style="color:#475569;line-height:1.6;">A qualified lead was automatically listed on the marketplace.</p>
        <div style="background:#fefce8;border-radius:12px;padding:20px;margin:16px 0;border-left:4px solid #eab308;">
          <p style="margin:4px 0;color:#0f172a;"><strong>Homeowner:</strong> {homeowner_name}</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Readiness score:</strong> {readiness_score}/100</p>
          <p style="margin:4px 0;color:#0f172a;"><strong>Listed price:</strong> ${price_dollars:.0f}</p>
        </div>""",
        cta_label="View on Marketplace",
        cta_url=f"{FRONTEND_URL}/app/marketplace",
    )
    return _send(email, f"💰 Lead listed on marketplace — ${price_dollars:.0f}", html)


def send_lead_sold(email: str, agent_name: str, payout_dollars: float) -> bool:
    """Agent notification when their marketplace listing sells."""
    html = _html_email(
        "Your lead sold!",
        f"""<p style="color:#475569;line-height:1.6;">Great news, {agent_name}! A buyer purchased your lead listing.</p>
        <div style="background:#f0fdf4;border-radius:12px;padding:20px;margin:16px 0;border-left:4px solid #22c55e;">
          <p style="margin:8px 0;font-size:28px;font-weight:700;color:#16a34a;">+${payout_dollars:.0f}</p>
          <p style="margin:4px 0;color:#475569;font-size:13px;">Your 60% payout — processing now</p>
        </div>""",
        cta_label="View Payouts",
        cta_url=f"{FRONTEND_URL}/app/marketplace/purchases",
    )
    return _send(email, f"🎉 Your lead sold — ${payout_dollars:.0f} incoming", html)


def send_low_minutes_warning(email: str, first_name: str, minutes_remaining: int) -> bool:
    """Warning when workspace is below 20% minutes remaining."""
    html = _html_email(
        f"Running low on minutes, {first_name}",
        f"""<p style="color:#475569;line-height:1.6;">You have <strong>{minutes_remaining} minutes</strong> remaining this month.</p>
        <p style="color:#475569;line-height:1.6;">Upgrade now to keep your campaigns running without interruption.</p>""",
        cta_label="Upgrade Plan",
        cta_url=f"{FRONTEND_URL}/app/billing",
    )
    return _send(email, f"⚠️ Running low on minutes — {minutes_remaining} left", html)


def send_campaign_completed(email: str, agent_name: str, campaign_name: str, total_calls: int, connected: int, booked: int, listed: int) -> bool:
    """Agent notification when a campaign finishes."""
    connect_rate = round(connected / total_calls * 100) if total_calls else 0
    html = _html_email(
        f"Campaign complete — {campaign_name}",
        f"""<p style="color:#475569;line-height:1.6;">Your AI campaign has finished dialing all leads.</p>
        <div style="background:#f8fafc;border-radius:12px;padding:20px;margin:16px 0;display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div><p style="margin:0;font-size:24px;font-weight:700;color:#0f172a;">{total_calls}</p><p style="margin:4px 0 0;color:#64748b;font-size:12px;">Total Calls</p></div>
          <div><p style="margin:0;font-size:24px;font-weight:700;color:#2563eb;">{connect_rate}%</p><p style="margin:4px 0 0;color:#64748b;font-size:12px;">Connect Rate</p></div>
          <div><p style="margin:0;font-size:24px;font-weight:700;color:#16a34a;">{booked}</p><p style="margin:4px 0 0;color:#64748b;font-size:12px;">Appointments</p></div>
          <div><p style="margin:0;font-size:24px;font-weight:700;color:#d97706;">{listed}</p><p style="margin:4px 0 0;color:#64748b;font-size:12px;">Listed</p></div>
        </div>""",
        cta_label="View Dashboard",
        cta_url=f"{FRONTEND_URL}/app/dashboard",
    )
    return _send(email, f"✅ Campaign complete — {booked} appointments booked", html)


def send_referral_reward(email: str, referrer_name: str, referred_name: str) -> bool:
    """Notification when a referral converts and referrer earns free month."""
    html = _html_email(
        "You earned a free month!",
        f"""<p style="color:#475569;line-height:1.6;">Hi {referrer_name}, great news!</p>
        <p style="color:#475569;line-height:1.6;"><strong>{referred_name}</strong> just signed up and paid using your referral link. We've added <strong>1 free month</strong> to your account.</p>""",
        cta_label="Go to Dashboard",
        cta_url=f"{FRONTEND_URL}/app/dashboard",
    )
    return _send(email, "🎁 You earned a free month — A2Z Dialer", html)


def send_intake_alert(admin_email: str, homeowner_name: str, property_address: str, phone: str, readiness_score: float, zip_code: str) -> bool:
    subject = f"New homeowner intake: {homeowner_name} — {zip_code}"
    html = _html_email(
        "New homeowner intake received",
        f"Name: {homeowner_name}<br>Address: {property_address}<br>Phone: {phone}<br>Readiness Score: {readiness_score:.0f}/100<br>ZIP: {zip_code}",
        "Log in to A2Z Dialer to view the full intake and assign to an agent.",
    )
    return _send(admin_email, subject, html)
