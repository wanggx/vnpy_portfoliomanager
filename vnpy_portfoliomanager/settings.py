"""成交记录入库相关的配置项与校验。

配置与引擎共用同一个设置文件 ``~/.vntrader/portfolio_manager_setting.json``，
放在 ``sql`` 子字典里：

```json
{
    "timer_interval": 5,
    "capitals": {"组合A": 500000},
    "sql": {
        "enabled": true,
        "table": "vnpy_portfolio_trade",
        "auto_create": true,
        "max_rows": 5000
    }
}
```

默认值不落盘（未配置时用默认值），只有用户显式改过才会由引擎写回设置文件，
避免把默认值固化进用户的配置里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# 设置文件里的 sql 子字典
SETTING_SQL = "sql"
SETTING_SQL_ENABLED = "enabled"
SETTING_SQL_TABLE = "table"
SETTING_SQL_AUTO_CREATE = "auto_create"
SETTING_SQL_MAX_ROWS = "max_rows"

DEFAULT_TABLE = "vnpy_portfolio_trade"
DEFAULT_MAX_ROWS = 5000

# 表名/列名白名单：只允许字母数字下划线且不以数字开头，拼接进 SQL 前必须校验
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)


def validate_identifier(name: Any, label: str) -> str:
    """校验 SQL 标识符（表名/列名），返回反引号包裹的版本

    SqlApp 不做方言转换，标识符只能由调用方拼进 SQL，所以必须白名单校验防止注入。
    """
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise ValueError(
            f"非法 SQL 标识符 {label}={name!r}：只允许字母、数字、下划线且不以数字开头"
        )
    return f"`{name}`"


def _to_bool(value: Any, default: bool) -> bool:
    """把配置里的值转成 bool，无法识别时用默认值"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text: str = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
    return default


def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把配置里的值转成范围内的 int，无法识别时用默认值"""
    try:
        result: int = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


@dataclass
class SqlSettings:
    """成交记录入库配置"""

    enabled: bool = True
    table: str = DEFAULT_TABLE
    auto_create: bool = True
    max_rows: int = DEFAULT_MAX_ROWS

    def is_valid_table(self) -> bool:
        """配置的表名是否合法（不合法时仓储层回退到默认表名并提示）"""
        return bool(self.table) and IDENTIFIER_RE.match(self.table) is not None


def parse_sql_settings(setting: dict[str, Any] | None) -> SqlSettings:
    """从设置文件的 ``sql`` 子字典解析配置，非法值一律回退默认值"""
    data: Any = (setting or {}).get(SETTING_SQL, {})
    if not isinstance(data, dict):
        data = {}

    return SqlSettings(
        enabled=_to_bool(data.get(SETTING_SQL_ENABLED), True),
        table=str(data.get(SETTING_SQL_TABLE) or DEFAULT_TABLE).strip(),
        auto_create=_to_bool(data.get(SETTING_SQL_AUTO_CREATE), True),
        max_rows=_to_int(data.get(SETTING_SQL_MAX_ROWS), DEFAULT_MAX_ROWS, 1, 1_000_000),
    )
