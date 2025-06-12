import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from zulip_bots.lib import BotHandler
from bot_config import ADMINS
import re
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables from .env (if running locally)
load_dotenv()

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Helper function to get all users from Supabase
def get_all_users():
    users = supabase.table('clock_entries').select('user_email').distinct().execute()
    return [user['user_email'] for user in users.data]

# Helper function to get the user sessions from Supabase
def get_user_sessions(user, since=None):
    query = supabase.table('clock_entries').select('*').eq('user_email', user)
    if since:
        query = query.gte('timestamp', since)
    return query.execute().data

# Helper function to log an event (clock-in or clock-out) to Supabase
def log_event(user, event):
    ts = datetime.now(ZoneInfo("UTC")).isoformat()
    supabase.table('clock_entries').insert([
        {'user_email': user, 'action': event, 'timestamp': ts}
    ]).execute()

# Function to generate the stats for the period (day, week, month, year, all)
def generate_stats(period="day", target_user=None):
    now = datetime.now(ZoneInfo("UTC"))
    since = None
    if period == "day":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    
    users = [target_user] if target_user else get_all_users()
    title = period.title() if period != "all" else "All time"

    lines = [f"⏰ Clock Stats ({title}):"]
    for u in users:
        total, last = 0.0, None
        for ev, ts in get_user_sessions(u, since.isoformat() if since else None):
            tdt = datetime.fromisoformat(ts)
            if ev == "in":
                last = tdt
            elif ev == "out" and last:
                total += (tdt - last).total_seconds() / 3600
                last = None
        lines.append(f"- {u}: {total:.2f} hrs")

    return "\n".join(lines)

# Helper function to format timestamps
def fmt_multi(ts):
    fmt = "%Y-%m-%d %H:%M:%S"
    zones = [
        ("UTC", "UTC"),
        ("NYC", "America/New_York"),
        ("IST", "Asia/Kolkata"),
        ("NPT", "Asia/Kathmandu"),
    ]
    parts = []
    for abbr, z in zones:
        parts.append(f"{abbr} {ts.astimezone(ZoneInfo(z)).strftime(fmt)}")
    return " | ".join(parts)

# Helper function to strip Zulip mentions from the message
def strip_mention(token):
    raw = token.lstrip("@*<").rstrip("*>")
    return raw.split("|", 1)[0]

# Main Handler Class
class Handler:
    def usage(self) -> str:
        return (
            "Commands:\n"
            " • in / clock in                  — record your start time\n"
            " • out / clock out                — record your end time\n"
            " • stats [day|week|month|year|all] — team totals\n"
            " • report @User [day|week|month|year|all]         — individual totals (admin)\n"
            " • report @User <N> weeks/months/years — last N units (admin)\n"
            " • help                           — show this message"
        )

    def handle_message(self, message, bot_handler: BotHandler) -> None:
        text = message["content"].strip()
        parts = text.split()
        if not parts:
            return
        cmd = parts[0].lower()
        sender_name = message["sender_full_name"]
        sender_email = message["sender_email"]

        if cmd in ("in", "clock") and len(parts) > 1 and parts[1].lower() == "in":
            now = datetime.now(ZoneInfo("UTC"))
            log_event(sender_email, "in")
            bot_handler.send_reply(
                message,
                f"✅ {sender_name} CLOCK IN at:\n{fmt_multi(now)}"
            )
            return

        if cmd in ("out", "clock") and len(parts) > 1 and parts[1].lower() == "out":
            now = datetime.now(ZoneInfo("UTC"))
            log_event(sender_email, "out")
            bot_handler.send_reply(
                message,
                f"⏱️ {sender_name} CLOCK OUT at:\n{fmt_multi(now)}"
            )
            return

        if cmd == "stats":
            period = parts[1].lower() if len(parts) > 1 else "day"
            if period not in ("day", "week", "month", "year", "all"):
                return bot_handler.send_reply(
                    message,
                    "Invalid period. Use: day, week, month, year, or all."
                )
            bot_handler.send_reply(message, generate_stats(period))
            return

        if cmd == "report":
            if sender_email not in ADMINS:
                return bot_handler.send_reply(
                    message,
                    "❌ You’re not authorized to run individual reports."
                )
            m = re.search(r"@\*\*(.*?)\*\*", text)
            if not m:
                return bot_handler.send_reply(
                    message,
                    "Usage: report @**User** [day|week|month|year|all] or report @**User** <N> units"
                )
            user = strip_mention(m.group(0))
            last = parts[-1].lower()

            if last in ("day", "week", "month", "year", "all"):
                bot_handler.send_reply(
                    message,
                    generate_stats(last, target_user=user)
                )
                return

            try:
                n = int(parts[-2])
                unit = parts[-1].lower()
                now = datetime.now(ZoneInfo("UTC"))
                if unit.startswith("week"):
                    since = now - timedelta(weeks=n)
                elif unit.startswith("month"):
                    since = now - timedelta(days=30 * n)
                elif unit.startswith("year"):
                    since = now - timedelta(days=365 * n)
                else:
                    raise ValueError
            except Exception:
                return bot_handler.send_reply(
                    message,
                    "Usage: report @**User** <N> weeks/months/years"
                )

            lines = [f"⏰ Report for {user} since {since.date()}:"] 
            total, last_in = 0.0, None
            for ev, ts in get_user_sessions(user, since.isoformat()):
                tdt = datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo("UTC"))
                lines.append(f" • {ev.upper():5s} @ {fmt_multi(tdt)}")
                if ev == "in":
                    last_in = tdt
                elif ev == "out" and last_in:
                    total += (tdt - last_in).total_seconds() / 3600
                    last_in = None
            lines.append(f"Total: {total:.2f} hrs")
            bot_handler.send_reply(message, "\n".join(lines))
            return

        if cmd == "help":
            return bot_handler.send_reply(message, self.usage())

        bot_handler.send_reply(message, "❓ Unknown command. Type `help` for usage.")

handler_class = Handler
