"""系統配置管理服務"""
import json
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .. import models


# ── RAG Prompt 預設值（與程式碼內 hardcode 完全一致，確保重置後行為不變）──

DEFAULT_RAG_SYSTEM_PROMPT = (
    "請務必使用「繁體中文（台灣）」回答，嚴格禁止簡體中文。"
    "所有說明文字 —— 包括表格與條列裡的內容 —— 都必須以繁體中文撰寫；"
    "引用英文來源時要翻譯成中文再寫入，僅規範代號、單位與專有名詞"
    "（如 Procedure I、METHOD 514.8、40 psig）保留原文。"
    "嚴禁整段照抄英文原文充當說明。避免輸出任何控制標記或思考過程。"
)

DEFAULT_RAG_USER_TEMPLATE = """\
你是一位文件問答助理，只能根據「可用段落」作答。
原則：
- 僅引用與使用者問題直接相關的段落內容，其餘無關資訊請忽略。
- 盡可能完整重現參考資料中的細節（如背景、限制、程序、數值、條件），並保持語意清楚。
- 「完整」指的是「不遺漏來源有的東西」，不是「把內容補齊」。嚴禁加入來源沒有的
  說明、定義、用途或背景知識。例如來源只給「01 | CPU」這樣的代號對照時，就只能
  寫出代號與名稱，不可補上「CPU 是中央處理器，負責執行運算」這類你自己知道的解釋
  ——使用者是拿這些內容去對照規範文件的，多出來的一句話會讓他找不到出處、
  也無從判斷那是文件寫的還是你加的。
- 寧可少寫也不要補寫：來源沒提到的就不要提。若你認為某項資訊對理解有幫助但來源
  沒有，請直接省略，不要以「一般而言」「通常」「即」等方式帶入外部知識。
- 回答完問題問的重點後，若來源中還有「同一測試方法」的其他重要執行條件（前置
  溫度、持續時間、週期數、設備與量測要求、注意事項等），請以「補充條件」小節
  逐項簡列並各自標註來源。這不牴觸「僅引用直接相關」——同一測試的執行條件視為
  相關；但嚴禁為湊篇幅引入其他測試方法的內容，也嚴禁在補充條件裡塞入外部知識。
- 全文一律以繁體中文改寫，包括表格與條列內的說明文字；規範代號、單位與專有
  名詞（如 Procedure I、METHOD 514.8、40 psig）保留原文即可。嚴禁整段照抄
  英文原文充當說明。
- 必須「涵蓋所有與問題主題相關的來源」，不可只回報最相關的一個：若有多個來源各自描述「不同但同屬問題主題」的項目（例如同一類測試下的不同子測試），必須逐一完整列出每一個，並分別標註各自來源；切勿在找到第一個符合的來源後就停止。
- 每個[來源]的資訊相互獨立，嚴格禁止跨來源拼湊細節（例如：不可將[來源2]的數值或條件套用到[來源1]的測試項目上）。
- 「涵蓋所有相關來源」與「禁止跨來源拼湊」並不衝突：前者要求把每個相關來源都各自獨立列出，後者要求每個項目的數值與條件只能取自它自己的來源；兩者必須同時遵守。
- 若多個來源涉及相似但不同的測試項目或主題，必須分開描述並明確標示各自來源，不可合併成同一段落。
- 標記「⚠️ 頁距 N，可能為不同章節」的來源極可能屬於不同測試項目：若其內容與問題主題不完全吻合，優先捨棄該來源；若仍引用，必須獨立描述並加以說明其來自不同章節，不可將其數值或條件與其他來源混用。
- 在回答文字中以 [來源1][來源3] 標示引用來源，可於同一句結尾列出多個來源。
- 若來源段落本身是「表格」（markdown 管線表格 `| ... |` 或 HTML `<table>`），回答時請「原樣以 markdown 表格呈現」該表格內容，保留欄位標題與每一列，不要壓平成純文字或條列；可在表格前後加一兩句說明並標註來源。
- 嚴禁「造出沒有內容的表格」：只有當來源本身就有數值或具體內容時才建表。若來源只是列出「測試前需指定的項目名稱」（例如規範中的 INFORMATION REQUIRED / 需在測試計畫中規定的清單），請用一句話說明「以下為測試計畫中須事先指定的項目」再條列項目名稱即可，不得為它們補上「需依測試計畫指定」「需根據測試項目需求設定」之類的空白欄位——每列都是同一句空話的表格等於沒有資訊。
- 若使用者問的是具體數值（條件、參數、濃度、溫度、時間、公差等），而可用段落中確實沒有數值，請明講「可用段落只列出需指定的項目，未含實際數值」，並指出數值可能所在的章節或建議的查詢方式，不要用空表格充數。
- 若所有段落皆無法回答，請明確回覆「查無相關資料」，並建議提供更多上下文。
- 參考對話歷史理解追問脈絡，但答案必須來自可用段落。
{{history}}
使用者問題：
{{question}}

可用段落：
{{context}}

請以下列格式輸出：
回答：
<可多段、條列或 markdown 表格；來源為表格時優先用 markdown 表格保留欄列，需保持細節並標註來源>
參考來源：
- [來源X] <此來源提供的重點>\
"""

# 預設配置值
DEFAULT_VECTOR_CONFIG = {
    "overlap_chars": 250,  # 向量塊重疊字符數（0 表示取消）
    "max_chars": 1800,  # 向量塊最大字符數
    # 向量匹配閾值 —— 實際上是無效的，保留 0.3 只是為了不動既有行為。
    #
    # qwen3-embedding:8b 的向量已 L2 正規化，餘弦分數分佈極窄，離題問題也遠高
    # 於 0.3（實測「紅燒獅子頭的食譜」top5 相似度 0.345~0.353）。
    # golden set 30 題掃描：
    #     0.0（等於關掉） hit@5 0.733  MRR 0.549
    #     0.3（現行）     hit@5 0.733  MRR 0.549   <- 與關掉完全相同
    #     0.55           hit@5 0.733  MRR 0.549   <- 外部審查建議值，同樣無差異
    #     0.65           hit@5 0.700  MRR 0.533   <- 開始砍到正解
    #
    # 也就是說：調高沒有好處，再高就開始傷害。真正在擋不相關結果的是
    # RAG_LOWCONF_CE_THRESHOLD（cross-encoder gate），不是這個值。
    # 換 embedding 模型後這個數字的意義會完全改變，不要把它當成有效的保護。
    "min_similarity_score": 0.3,
    "default_top_k": 5,  # 預設返回來源數量
    "search_multiplier": 10,  # 搜索倍數
}


class SystemConfigService:
    """系統配置服務"""

    def __init__(self, db: Session):
        self.db = db

    def get_config(self, key: str, default: Any = None) -> Any:
        """獲取配置值"""
        config = self.db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if config:
            try:
                return json.loads(config.value)
            except json.JSONDecodeError:
                return config.value
        return default

    def set_config(self, key: str, value: Any, description: Optional[str] = None) -> None:
        """設置配置值"""
        config = self.db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()

        # 將值轉換為 JSON 字符串
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        else:
            value_str = json.dumps(value)

        if config:
            config.value = value_str
            if description:
                config.description = description
        else:
            config = models.SystemConfig(
                key=key,
                value=value_str,
                description=description
            )
            self.db.add(config)

        self.db.commit()

    def get_vector_config(self) -> Dict[str, Any]:
        """獲取向量配置"""
        saved_config = self.get_config("vector_config")
        if saved_config:
            # 合併預設值和已保存的值
            config = DEFAULT_VECTOR_CONFIG.copy()
            config.update(saved_config)
            return config
        return DEFAULT_VECTOR_CONFIG.copy()

    def update_vector_config(self, config: Dict[str, Any]) -> None:
        """更新向量配置"""
        # 驗證配置值
        if "overlap_chars" in config:
            overlap = config["overlap_chars"]
            if not isinstance(overlap, int) or overlap < 0:
                raise ValueError("overlap_chars 必須是非負整數")

        if "max_chars" in config:
            max_chars = config["max_chars"]
            if not isinstance(max_chars, int) or max_chars <= 0:
                raise ValueError("max_chars 必須是正整數")

        if "min_similarity_score" in config:
            score = config["min_similarity_score"]
            if not isinstance(score, (int, float)) or not (0 <= score <= 1):
                raise ValueError("min_similarity_score 必須在 0-1 之間")

        if "default_top_k" in config:
            top_k = config["default_top_k"]
            if not isinstance(top_k, int) or top_k <= 0:
                raise ValueError("default_top_k 必須是正整數")

        if "search_multiplier" in config:
            multiplier = config["search_multiplier"]
            if not isinstance(multiplier, int) or multiplier <= 0:
                raise ValueError("search_multiplier 必須是正整數")

        self.set_config("vector_config", config, "向量處理相關配置")

    # ── RAG Prompt ──

    def get_rag_prompts(self) -> Dict[str, Any]:
        """回傳目前生效的 RAG prompts，並標記是否使用預設值。"""
        system_prompt = self.get_config("rag_system_prompt")
        user_template = self.get_config("rag_user_template")
        is_default = system_prompt is None and user_template is None
        return {
            "system_prompt": system_prompt if system_prompt is not None else DEFAULT_RAG_SYSTEM_PROMPT,
            "user_template": user_template if user_template is not None else DEFAULT_RAG_USER_TEMPLATE,
            "is_default": is_default,
        }

    def update_rag_prompts(
        self,
        system_prompt: Optional[str] = None,
        user_template: Optional[str] = None,
    ) -> None:
        """儲存自訂 RAG prompts（僅更新有傳入的欄位）。"""
        if system_prompt is not None:
            self.set_config("rag_system_prompt", system_prompt, "RAG 系統提示詞")
        if user_template is not None:
            if "{{question}}" not in user_template or "{{context}}" not in user_template:
                raise ValueError("提示詞模板必須包含 {{question}} 與 {{context}} 佔位符")
            self.set_config("rag_user_template", user_template, "RAG 查詢提示詞模板")

    def reset_rag_prompts(self) -> None:
        """刪除自訂值，恢復為程式碼預設 prompt（不修改任何邏輯）。"""
        for key in ("rag_system_prompt", "rag_user_template"):
            row = self.db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
            if row:
                self.db.delete(row)
        self.db.commit()

    # ── LLM Provider ──

    LLM_CONFIG_KEYS = (
        "llm.provider",
        "llm.model",
        "embedding.provider",
        "embedding.model",
        "vision.provider",
        "vision.model",
        "gemini.api_key",
    )

    def get_llm_provider_config(self) -> Dict[str, Any]:
        """Read provider config from DB (if any); caller merges with settings defaults."""
        out: Dict[str, Any] = {}
        for key in self.LLM_CONFIG_KEYS:
            v = self.get_config(key)
            if v is not None:
                out[key] = v
        return out

    def update_llm_provider_config(self, payload: Dict[str, Any]) -> None:
        """Update LLM/embedding provider keys. Only writes keys present in payload."""
        for key, desc in (
            ("llm.provider", "LLM 後端 (ollama|gemini)"),
            ("llm.model", "LLM 模型 ID"),
            ("embedding.provider", "Embedding 後端"),
            ("embedding.model", "Embedding 模型 ID"),
            ("vision.provider", "VL 後端 (目前僅 ollama)"),
            ("vision.model", "VL 模型 ID"),
            ("gemini.api_key", "Google Gemini API key"),
        ):
            if key in payload:
                value = payload[key]
                if value is None or (isinstance(value, str) and value.strip() == ""):
                    # Delete row (revert to .env default)
                    row = self.db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
                    if row:
                        self.db.delete(row)
                else:
                    self.set_config(key, value, desc)
        self.db.commit()

    # ── OCR Engine ──

    OCR_CONFIG_KEYS = (
        "ocr.engine",
        "ocr.model_tier",
        "ocr.version",
        "ocr.device",
    )

    def get_ocr_config(self) -> Dict[str, Any]:
        """Read OCR engine config from DB (if any); caller merges with settings defaults."""
        out: Dict[str, Any] = {}
        for key in self.OCR_CONFIG_KEYS:
            v = self.get_config(key)
            if v is not None:
                out[key] = v
        return out

    def update_ocr_config(self, payload: Dict[str, Any]) -> None:
        """Update OCR engine keys. Only writes keys present in payload; empty → revert to default."""
        for key, desc in (
            ("ocr.engine", "OCR 引擎 (rapid|pp_structure)"),
            ("ocr.model_tier", "OCR 模型等級 (mobile|server)"),
            ("ocr.version", "OCR 版本 (PP-OCRv4|PP-OCRv5)"),
            ("ocr.device", "OCR 裝置 (cpu|gpu)"),
        ):
            if key in payload:
                value = payload[key]
                if value is None or (isinstance(value, str) and value.strip() == ""):
                    row = self.db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
                    if row:
                        self.db.delete(row)
                else:
                    self.set_config(key, value, desc)
        self.db.commit()

    def get_all_configs(self) -> Dict[str, Any]:
        """獲取所有配置"""
        configs = self.db.query(models.SystemConfig).all()
        result = {}
        for config in configs:
            try:
                result[config.key] = json.loads(config.value)
            except json.JSONDecodeError:
                result[config.key] = config.value
        return result
