"""Model-scoped GATT property definitions, independent of controller workers.

Definitions are shared by the public async client and dashboard controllers.
Inspecting this catalog performs no Bluetooth, cloud, HTTP or thread I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .es5_radar import REPORT_MASK, REPORT_MINUTES, hex_series
from .standard_auth import DirectError, encode_value


@dataclass(frozen=True)
class Property:
    name: str
    label: str
    siid: int
    piid: int
    format: str
    writable: bool = False
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    optional: bool = False
    allowed_mask: int | None = None
    notification_only: bool = False
    allowed_values: tuple[int, ...] = ()

    def validate(self, value: Any, *, reading: bool = False) -> None:
        if not self.writable and not reading:
            raise DirectError("此欄位目前僅供讀取。")
        encode_value(value, self.format)
        if self.name in {"motion_threshold", "presence_threshold"} and hex_series(value) is None:
            raise DirectError("閾值需包含 32 個十六進位位元組。")
        if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
            raise DirectError("設定值超出 ES5BB 規格範圍。")
        if not reading and self.allowed_mask is not None and value & ~self.allowed_mask:
            raise DirectError("設定包含尚未定義的保留位元，無法寫入。")
        if self.allowed_values and value not in self.allowed_values:
            raise DirectError("裝置回報未定義的列舉值，不視為有效讀值。")


PROPERTIES = [
    Property("occupancy_status", "人體存在", 2, 1078, "uint8", minimum=0, maximum=1, choices=("無人", "有人")),
    Property("occupancy_close_range", "近距區間", 5, 1019, "uint8", minimum=0, maximum=1, choices=("無人", "有人")),
    Property("illuminance", "照度", 2, 1005, "float"),
    Property("battery_level", "電池電量", 7, 1003, "uint8", minimum=0, maximum=100),
    Property("no_one_determine_time", "無人判定時間", 2, 5, "uint16", True, 0, 10000),
    Property("function_mode", "手動關燈處理模式", 6, 42, "uint8", True, 0, 2, ("停用", "快速", "睡眠")),
    Property("sensitivity_level", "整體靈敏度", 6, 11, "uint8", True, 0, 3, ("低", "中", "高", "自訂")),
    Property("motion_energy", "觸發有人能量", 6, 4, "string"),
    Property("presence_energy", "維持有人能量", 6, 5, "string"),
    Property("human_sensor_bitmaps", "感應結果位圖", 6, 8, "string"),
    Property("cmd_to_device", "裝置命令狀態（唯讀）", 6, 9, "uint32"),
    Property("detect_region", "雷達感應區段", 6, 7, "uint32", True, 1, 0xFFFFFFFF),
    Property("interest_region", "近距關注區段", 6, 17, "uint32", True, 0, 0xFFFFFFFF),
    Property("motion_threshold", "觸發有人閾值", 6, 2, "string", True),
    Property("presence_threshold", "維持有人閾值", 6, 3, "string", True),
    Property("radar_motion_sens", "雷達觸發靈敏度", 6, 14, "uint8", True, 0, 3, ("自訂", "低", "中", "高")),
    Property("radar_presence_sens", "雷達維持靈敏度", 6, 15, "uint8", True, 0, 3, ("自訂", "低", "中", "高")),
    Property("pir_sensitivity", "紅外線靈敏度", 6, 13, "uint8", True, 0, 3, ("自訂", "低", "中", "高")),
    Property("pir_energy", "PIR 紅外線原始能量", 6, 18, "int32", minimum=-1000, maximum=1000, optional=True, notification_only=True),
    Property("pir_threshold", "PIR 紅外線閾值", 6, 19, "uint8", True, 0, 255, optional=True),
    Property("detect_mode", "感應模式", 6, 12, "uint8", True, 0, 2, ("僅雷達", "雷達＋紅外線標準", "雷達＋紅外線高效能")),
    Property("ai_anti_interference", "AI 抗干擾等級", 6, 16, "uint8", True, 0, 8),
    Property("interest_enable", "近距區間啟用", 6, 72, "bool", True),
    Property("sleep_determine_time", "睡眠模式無人判定時間", 6, 43, "uint16", True, 0, 65535),
    Property("connection_wake_time", "斷線後可直連時間", 6, 62, "uint8", True, 0, 3, ("短時間", "長時間", "超長時間", "持續")),
    Property("device_conn_tick", "直連計時", 6, 64, "uint8"),
    Property("power_mode", "供電模式", 6, 63, "uint8", minimum=0, maximum=2, choices=("電池", "光能", "外部供電")),
    Property("radar_frame_rate", "雷達幀率", 6, 65, "uint8", True, 1, 20),
    Property("convergence_level", "觸發收斂等級", 6, 66, "uint8", True, 0, 50, optional=True),
    Property("presence_response_time", "存在檢測響應時間", 6, 67, "uint8", True, 1, 8),
    Property("default_motion_threshold", "預設觸發閾值", 6, 45, "string"),
    Property("default_presence_threshold", "預設維持閾值", 6, 46, "string"),
    Property("indicator_light", "指示燈", 3, 1, "bool", True, optional=True),
    Property("no_one_duration", "無人持續時間上報（分鐘）", 2, 1082, "uint8", optional=True, allowed_values=REPORT_MINUTES),
    Property("has_someone_duration", "有人持續時間上報（分鐘）", 2, 1081, "uint8", optional=True, allowed_values=REPORT_MINUTES),
    Property("device_timestamp", "裝置時間戳記", 6, 1, "uint32", optional=True),
    Property("motion_log", "雷達動作紀錄（原始字串）", 6, 6, "string", optional=True),
    Property("function_mode_activation", "手動關燈模式啟動狀態（原始值）", 6, 44, "uint32", optional=True),
    Property("no_one_report_mask", "無人持續時間上報門檻", 6, 60, "uint16", True, 0, 65535, optional=True, allowed_mask=REPORT_MASK),
    Property("someone_report_mask", "有人持續時間上報門檻", 6, 61, "uint16", True, 0, 65535, optional=True, allowed_mask=REPORT_MASK),
    Property("manual_off_illumination_delta", "關燈偵測照度差", 6, 68, "uint16", True, 0, 1000, optional=True),
    # v113 writes this as uint16 but the published ES5 spec declares uint8.
    # Keep it read-only until a real authenticated wire value settles the type.
    Property("manual_off_detection_seconds", "關燈偵測時間（型別待實機確認）", 6, 69, "uint8", minimum=0, maximum=255, optional=True),
    Property("manual_off_activation_lux", "關燈模式啟用照度上限", 6, 70, "uint16", True, 0, 1000, optional=True),
    Property("manual_off_deactivation_lux", "關燈模式解除照度門檻", 6, 71, "uint16", True, 0, 1000, optional=True),
    Property("manual_off_deactivation_seconds", "關燈模式解除判定時間", 6, 73, "uint16", True, 0, 1000, optional=True),
    Property("manual_off_deactivation_delta_lux", "開燈偵測照度差", 6, 74, "uint16", True, 0, 1000, optional=True),
    Property("light_value_lux", "裝置照度判斷值", 6, 75, "uint16", minimum=0, maximum=1000, optional=True),
    Property("power_upload_type", "電量上報來源", 6, 76, "uint8", minimum=0, maximum=1, choices=("電池", "太陽能板"), optional=True),
    Property("solar_board_power", "太陽能板電量", 6, 77, "uint8", minimum=0, maximum=100, optional=True),
    Property("power_upload_fast_enable", "電量快速上報", 6, 78, "uint8", minimum=0, maximum=1, choices=("停用", "啟用"), optional=True),
    Property("manufacturer", "製造商", 1, 1, "string", optional=True),
    Property("device_model", "裝置型號", 1, 2, "string", optional=True),
    Property("device_id", "裝置識別碼", 1, 3, "string", optional=True),
    Property("firmware_revision", "規格韌體版本", 1, 4, "string", optional=True),
    Property("serial_number", "序號", 1, 5, "string", optional=True),
    Property("custom_event_argument", "裝置事件代碼", 5, 1, "uint8", minimum=0, maximum=8,
        choices=("重設事件", "關燈離開", "關燈退出", "關燈睡眠", "關燈退出（4）", "雷達底噪輕微異常", "雷達底噪嚴重異常", "雷達底噪正常", "雷達底噪錯誤"), optional=True),
]
CATALOG = {prop.name: prop for prop in PROPERTIES}
PAIR_CATALOG = {(prop.siid, prop.piid): prop for prop in PROPERTIES}


PROTOCOL_MODELS = {"MS1BB": "linp.magnet.m1", "KS2BB": "linp.sensor_ht.ks2bb", "HS3BB": "linp.motion.hs1bb2"}


@dataclass(frozen=True)
class SensorProperty:
    name: str
    label: str
    siid: int
    piid: int
    format: str
    minimum: int | None = None
    maximum: int | None = None
    step: int = 1
    choices: tuple[str, ...] = ()
    unit: str | None = None
    notify: bool = False
    writable: bool = True

    def validate(self, value: Any) -> None:
        encode_value(value, self.format)
        if not self.writable:
            raise DirectError("此屬性僅供讀取。")
        if self.choices and (type(value) is not int or value not in range(len(self.choices))):
            raise DirectError("設定值不在此型號的列舉範圍。")
        if self.minimum is not None and (value < self.minimum or value > self.maximum or (value - self.minimum) % self.step):
            raise DirectError("設定值超出此型號的範圍或步進。")

    def public(self) -> dict[str, Any]:
        return {"label": self.label, "siid": self.siid, "piid": self.piid, "format": self.format,
                "writable": self.writable, "minimum": self.minimum, "maximum": self.maximum,
                "step": self.step, "choices": list(self.choices), "unit": self.unit,
                "status": "unread", "value": None, "updated_at": None}


PROFILES = {
    "MS1BB": (),  # Released v1 has no GATT settings; never substitute debug v2.
    "KS2BB": (
        SensorProperty("button_mode", "按鍵模式", 6, 1, "uint8", choices=("多擊模式", "快速單擊"), notify=True),
        SensorProperty("temperature_unit", "顯示溫標", 6, 2, "uint8", choices=("攝氏 °C", "華氏 °F")),
    ),
    "HS3BB": (
        SensorProperty("detection_interval", "移動偵測間隔", 2, 2, "uint16", 10, 50, 10, unit="秒", notify=True),
        SensorProperty("no_motion_time", "自訂無人時間", 2, 1053, "uint16", 1, 60, unit="分鐘", notify=True),
        SensorProperty("indicator_light", "指示燈", 4, 1, "bool", notify=True),
        SensorProperty("no_motion_timeout_1", "無人時間參數 1", 5, 1, "uint16", 60, 3600, unit="秒"),
        SensorProperty("no_motion_timeout_2", "無人時間參數 2", 5, 2, "uint16", 60, 3600, unit="秒"),
        SensorProperty("pir_single_report", "PIR 單次回報", 5, 3, "bool"),
    ),
}
