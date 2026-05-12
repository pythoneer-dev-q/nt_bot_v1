import os
from ast import literal_eval
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []

    parsed_value = value.strip()
    if parsed_value.startswith("[") and parsed_value.endswith("]"):
        try:
            loaded = literal_eval(parsed_value)
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        except (ValueError, SyntaxError):
            pass

    return [item.strip() for item in parsed_value.split(",") if item.strip()]


def normalize_channel_ref(raw_value: str) -> str:
    value = raw_value.strip().strip("'\"")
    if not value:
        return value

    if value.startswith(("https://t.me/", "http://t.me/")):
        value = value.split("t.me/", 1)[1]
    elif value.startswith("t.me/"):
        value = value.split("t.me/", 1)[1]

    value = value.strip("/")
    if value.startswith("@"):
        return value
    if value.startswith("-100") or value.lstrip("-").isdigit():
        return value
    return f"@{value}"


def _parse_int_csv(value: str | None) -> set[int]:
    return {int(item) for item in _parse_csv(value)}


def _parse_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        loaded = literal_eval(value)
    except (ValueError, SyntaxError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass(slots=True)
class Settings:
    bot_token: str
    marzban_url: str
    marzban_username: str
    marzban_password: str
    mongo_dsn: str
    mongo_db: str
    admin_ids: set[int]
    help_url: str
    user_agreement_url: str | None
    privacy_policy_url: str | None
    menu_image_url: str | None
    support_url: str | None
    support_username: str | None
    donate_url: str | None
    donate_card: str | None
    project_name: str
    trial_days: int
    trial_limit_gb: int
    free_renew_window_hours: int
    free_renew_cooldown_days: int
    backup_enabled: bool
    backup_dir: str
    backup_interval_hours: int
    backup_keep_days: int
    expired_subscription_check_minutes: int
    expired_subscription_note: str
    expired_subscription_proxies: dict
    expired_subscription_inbounds: dict
    required_channels: list[str]
    broadcast_batch_size: int
    broadcast_concurrency: int
    redis_dsn: str | None
    debug: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN") or os.getenv("M_TOKEN")
        if not bot_token:
            raise ValueError("BOT_TOKEN is not set in .env")

        marzban_url = os.getenv("M_URL", "http://127.0.0.1:8000")
        marzban_username = os.getenv("M_USERNAME")
        marzban_password = os.getenv("M_PASSWORD")
        if not marzban_username or not marzban_password:
            raise ValueError("M_USERNAME and M_PASSWORD are not set in .env")

        return cls(
            bot_token=bot_token,
            marzban_url=marzban_url,
            marzban_username=marzban_username,
            marzban_password=marzban_password,
            mongo_dsn=os.getenv("MONGO_DSN", "mongodb://127.0.0.1:27017"),
            mongo_db=os.getenv("MONGO_DB", "s_db"),
            admin_ids=_parse_int_csv(os.getenv("ADMIN_IDS")),
            help_url=os.getenv("HELP_URL", "https://your-guide-link.com"),
            user_agreement_url=os.getenv("USER_AGREEMENT_URL") or None,
            privacy_policy_url=os.getenv("PRIVACY_POLICY_URL") or None,
            menu_image_url=os.getenv("MENU_IMAGE_URL") or None,
            support_url=os.getenv("SUPPORT_URL") or None,
            support_username=os.getenv("SUPPORT_USERNAME") or None,
            donate_url=os.getenv("DONATE_URL") or None,
            donate_card=os.getenv("DONATE_CARD") or None,
            project_name=os.getenv("PROJECT_NAME", "Not A VPN"),
            trial_days=_parse_int(os.getenv("TRIAL_DAYS"), 30),
            trial_limit_gb=_parse_int(os.getenv("TRIAL_LIMIT_GB"), 50),
            free_renew_window_hours=_parse_int(os.getenv("FREE_RENEW_WINDOW_HOURS"), 24),
            free_renew_cooldown_days=_parse_int(os.getenv("FREE_RENEW_COOLDOWN_DAYS"), 30),
            backup_enabled=_parse_bool(os.getenv("BACKUP_ENABLED"), True),
            backup_dir=os.getenv("BACKUP_DIR", "backups"),
            backup_interval_hours=_parse_int(os.getenv("BACKUP_INTERVAL_HOURS"), 24),
            backup_keep_days=_parse_int(os.getenv("BACKUP_KEEP_DAYS"), 14),
            expired_subscription_check_minutes=_parse_int(os.getenv("EXPIRED_SUBSCRIPTION_CHECK_MINUTES"), 60),
            expired_subscription_note=os.getenv(
                "EXPIRED_SUBSCRIPTION_NOTE",
                "Subscription expired. Open the bot and renew your subscription.",
            ),
            expired_subscription_proxies=_parse_json_dict(os.getenv("EXPIRED_SUBSCRIPTION_PROXIES")),
            expired_subscription_inbounds=_parse_json_dict(os.getenv("EXPIRED_SUBSCRIPTION_INBOUNDS")),
            required_channels=[normalize_channel_ref(item) for item in _parse_csv(os.getenv("REQUIRED_CHANNELS"))],
            broadcast_batch_size=_parse_int(os.getenv("BROADCAST_BATCH_SIZE"), 100),
            broadcast_concurrency=_parse_int(os.getenv("BROADCAST_CONCURRENCY"), 20),
            redis_dsn=os.getenv("REDIS_DSN") or None,
            debug=_parse_bool(os.getenv("DEBUG"), False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


settings = Settings.from_env()
