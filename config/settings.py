import os
from ast import literal_eval
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _parse_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


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
    return {int(item) for item in _parse_csv(value) if item.isdigit() or (item.startswith("-") and item[1:].isdigit())}


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
    menu_description: str
    support_url: str | None
    support_username: str | None
    project_name: str
    premium_base_price_rub: float
    default_discount_percent: float
    free_device_limit: int
    premium_device_limit: int
    marzban_token: str | None
    pasarguard_free_group_name: str
    pasarguard_premium_group_name: str
    pasarguard_renew_group_name: str
    pasarguard_blocked_group_name: str
    overdraft_price_rub: float
    rolly_api_url: str
    rolly_api_key: str | None
    rolly_terminal_id: str | None
    rolly_signing_secret: str | None
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
    disable_logs: bool
    default_groups: list[int]

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN") or os.getenv("M_TOKEN")
        if not bot_token:
            raise ValueError("BOT_TOKEN is not set in .env")

        marzban_url = os.getenv("M_URL", "http://127.0.0.1:8000")
        marzban_username = os.getenv("M_USERNAME", "")
        marzban_password = os.getenv("M_PASSWORD", "")

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
            menu_description=os.getenv(
                "MENU_DESCRIPTION",
                "Быстрый, безопасный и приватный доступ в интернет без ограничений.",
            ),
            support_url=os.getenv("SUPPORT_URL") or None,
            support_username=os.getenv("SUPPORT_USERNAME") or None,
            project_name=os.getenv("PROJECT_NAME", "VPN"),
            premium_base_price_rub=_parse_float(os.getenv("PREMIUM_BASE_PRICE_RUB"), 199.0),
            default_discount_percent=_parse_float(os.getenv("DEFAULT_DISCOUNT_PERCENT"), 10.0),
            free_device_limit=_parse_int(os.getenv("FREE_DEVICE_LIMIT") or os.getenv("DEFAULT_FREE_DEVICE_LIMIT"), 1),
            premium_device_limit=_parse_int(os.getenv("PREMIUM_DEVICE_LIMIT") or os.getenv("DEFAULT_PREMIUM_DEVICE_LIMIT"), 3),
            marzban_token=os.getenv("M_TOKEN") or None,
            pasarguard_free_group_name=os.getenv("PASARGUARD_FREE_GROUP_NAME", "aska_free"),
            pasarguard_premium_group_name=os.getenv("PASARGUARD_PREMIUM_GROUP_NAME", "aska_premium"),
            pasarguard_renew_group_name=os.getenv("PASARGUARD_RENEW_GROUP_NAME", "aska_renew"),
            pasarguard_blocked_group_name=os.getenv("PASARGUARD_BLOCKED_GROUP_NAME", "aska_blocked"),
            overdraft_price_rub=_parse_float(os.getenv("OVERDRAFT_PRICE_RUB"), 29.0),
            rolly_api_url=os.getenv("ROLLY_API_URL", "https://api.rollypay.io").rstrip("/"),
            rolly_api_key=os.getenv("ROLLY_API_KEY") or None,
            rolly_terminal_id=os.getenv("ROLLY_TERMINAL_ID") or None,
            rolly_signing_secret=os.getenv("ROLLY_SIGNING_SECRET") or None,
            trial_days=_parse_int(os.getenv("TRIAL_DAYS"), 14),
            trial_limit_gb=_parse_int(os.getenv("TRIAL_LIMIT_GB"), 100),
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
            disable_logs=_parse_bool(os.getenv("DISABLE_LOGS"), False),
            default_groups=[int(x) for x in _parse_csv(os.getenv("DEFAULT_GROUPS", "1,4")) if x.strip().isdigit()],
        )


settings = Settings.from_env()
