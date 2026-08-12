from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "AI Document V3"
    PROJECT_VERSION: str = "1.0.0"

    DATABASE_URL: str = "sqlite:///./doc_management.db"

    # Security: SECRET_KEY must be provided via environment variable
    SECRET_KEY: str = Field(
        ...,
        min_length=32,
        description="JWT secret key - MUST be set in .env file"
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30  # 30 minutes (recommended: 15-60)

    # Refresh Token Configuration
    # Allows users to stay logged in for extended periods without re-authentication
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 7 days (recommended: 7-30)

    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "Admin@123"
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"

    # Ollama 推理服務
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_LLM_MODEL: str = "qwen3:8b"
    OLLAMA_VISION_MODEL: str = "qwen2.5vl:7b"
    OLLAMA_EMBED_MODEL: str = "quentinz/bge-large-zh-v1.5:latest"
    OLLAMA_KEEP_ALIVE: str = "5m"
    OLLAMA_TIMEOUT: int = 120  # seconds
    # Optional generation controls (help avoid truncated answers)
    # -1 for unlimited tokens (Ollama default); increase context for long PDFs
    OLLAMA_NUM_PREDICT: int | None = -1
    # 8192 曾是全鏈路的瓶頸：因為它，RAG_MAX_CONTEXT_CHUNKS 被壓到 6、
    # RAG_CONTEXT_BUDGET_CHARS 壓到 6000、表格逐列增強只能做第一個來源，
    # 答案末端才會頻繁出現「另有 N 段因長度限制未展開」。
    # 24GB VRAM 跑 qwen3:8b @ 24k 綽綽有餘。放寬後務必用評估確認長脈絡下
    # 生成品質沒有退化（長 context 可能出現「中間遺失」）。
    OLLAMA_NUM_CTX: int | None = 24576
    # 嵌入模型的 ctx 與 chat 分開設：qwen3-embedding:8b 若用模型預設 32768，
    # KV cache 讓它吃到 10GB —— 8GB 卡連單獨載入都不行，直接溢出到 CPU。
    # 降到 4096 只剩 6.6GB，且對嵌入值零影響（實測與預設 ctx 的 cosine =
    # 1.000000；切塊上限 ~2000 字元，遠低於 4096）。
    OLLAMA_EMBED_NUM_CTX: int | None = 4096
    # Sampling and repetition controls (optional; set in .env if needed)
    # 生成的可重現性。
    #
    # 兩者原本都是 None，於是 Ollama 用模型自己的預設值，同一個問題每次的
    # 答案都不同。實測 golden set 連跑三次：0.600 / 0.600 / 0.667 ——
    # ±1 題是雜訊，而我一度把兩次 0.033 的變化當成真實效果解讀。
    #
    # temperature=0 讓取樣趨近貪婪、seed 固定殘餘的隨機性，兩者都需要。
    # 這是規範查詢系統：同一個問題本來就該給同一個答案，不需要多樣性。
    OLLAMA_TEMPERATURE: float | None = 0.0
    OLLAMA_SEED: int | None = 42
    OLLAMA_TOP_P: float | None = None
    OLLAMA_TOP_K: int | None = None
    OLLAMA_REPEAT_PENALTY: float | None = None
    OLLAMA_MIROSTAT: int | None = None  # 0/1/2
    OLLAMA_MIROSTAT_TAU: float | None = None
    OLLAMA_MIROSTAT_ETA: float | None = None
    # Optional comma-separated stop tokens, e.g.: "<|im_start|>,<|im_end|>,</s>"
    OLLAMA_STOP: str | None = None

    FILE_STORAGE_DIR: str = "./storage"
    PDF_STORAGE_DIR: str = "./storage/documents"
    PDF_TEMP_DIR: str = "./storage/tmp"
    FAISS_INDEX_PATH: str = "./storage/faiss_index.bin"

    # RAG 相關設定
    MIN_SIMILARITY_SCORE: float = 0.3  # 最低相似度分數閾值
    DEFAULT_TOP_K: int = 5  # 預設返回的來源數量
    SEARCH_MULTIPLIER: int = 10  # 搜尋倍數（實際搜尋 top_k * multiplier）

    # PDF 多頁 VL 分析上限（受模型 context window 限制）
    MAX_PDF_ANALYSIS_PAGES: int = 10

    # LLM provider 抽象：可獨立切換 LLM 與 embedding 的後端
    LLM_PROVIDER: str = "ollama"               # ollama | gemini
    LLM_MODEL: str | None = None               # 覆寫 OLLAMA_LLM_MODEL；空則用 provider 預設
    EMBEDDING_PROVIDER: str = "ollama"
    EMBEDDING_MODEL: str | None = None
    # Audit H1：embedding 前的防護式截斷字元上限。舊版硬編 450（為 bge-large 512-token
    # 上限而設），但預設 chunk 1800 字 → 只有前 25% 進向量、後段查不到。
    # 部署的 qwen3-embedding 上下文遠大於此，設 2000 可完整涵蓋 1800 字 chunk。
    # 若改用 bge-large 等 512-token 模型，請在 .env 把此值調回 ~450。
    EMBEDDING_MAX_CHARS: int = 2000
    # VL 視覺模型(目前只支援 ollama;Gemini vision 走 LLM_PROVIDER 那條)
    VISION_PROVIDER: str = "ollama"
    VISION_MODEL: str | None = None            # 覆寫 OLLAMA_VISION_MODEL;留空則沿用

    # Gemini / Google AI Studio
    GEMINI_API_KEY: str | None = None
    GEMINI_LLM_MODEL: str = "gemini-2.5-flash"
    GEMINI_EMBED_MODEL: str = "text-embedding-004"

    # KG 抽取
    KG_AUTO_EXTRACT: bool = True               # 文件 ingest 完成後自動跑 KG 抽取
    KG_MIN_CONFIDENCE: float = 0.3             # 低於此值的 LLM relation 不寫入
    # 用版面/字體式標題偵測（kg_headings）建立 method/section 節點與引用邊；
    # 關閉則退回 kg_structure 的純文字 regex 偵測。需重讀文件 PDF（document.pdf_path）。
    KG_HEADING_SECTIONS: bool = True

    # RAG「小找大」：命中小塊後，連同同文件前後 N 塊一起餵給 LLM，
    # 避免句子被切塊邊界截斷（搜尋精度用小塊、上下文完整性用擴展後的大塊）。
    RAG_NEIGHBOR_RADIUS: int = 1               # 0 = 關閉擴展；1 = 帶 k-1/k+1
    RAG_EXPAND_MAX_CHARS: int = 2000           # 單一來源擴展後的字數上限
    # 所有來源餵入 LLM 的總字數「上限」(實際還會依 num_ctx 動態夾限，見 effective_rag_budget)。
    # input+output 共用同一 context window，須留生成空間。8192 視窗下 6000 字安全。
    # 脈絡字數上限。6000 是 num_ctx=8192 時代的遺留值，而且當時把字元誤當 token
    # （英文語料實測 4.52 字元/token），等於只用到視窗的 14%。
    RAG_CONTEXT_BUDGET_CHARS: int = 20000
    RAG_OUTPUT_RESERVE_TOKENS: int = 2048    # 留給生成
    RAG_PROMPT_OVERHEAD_TOKENS: int = 1500   # system prompt + 模板 + 問題 + 對話歷史
    RAG_OUTPUT_RESERVE_CHARS: int = 3000       # 預留給「生成」的視窗額度（夾限時扣除）；調高以容納多來源長答案

    # OCR(PP-StructureV3)頁面影像大小。paddle CPU 在「大尺寸影像」上跑 layout/table 模型會
    # native segfault(實測 2200px 的頁面會整個 crash、1600px 正常)。把頁面影像上限壓到安全值。
    OCR_DPI: int = 150
    OCR_MAX_DIMENSION: int = 1600
    # 子進程隔離:每頁在 worker 子進程跑 PP-Structure,某頁 native segfault 不拖垮整份(崩潰頁退回 basic OCR)。
    OCR_SUBPROCESS_ISOLATION: bool = True
    OCR_WORKER_TIMEOUT: int = 3600              # 單次 worker 子進程逾時(秒)

    # OCR 引擎選擇(可在 admin 切換)。
    #  rapid        = 輕量 onnx 三件套(RapidLayout+RapidTable+RapidOCR);快(~4s/頁)、無 paddle/segfault。
    #  pp_structure = PaddleOCR PP-StructureV3(重、有 segfault 風險,保底用)。
    OCR_ENGINE: str = "rapid"                   # rapid | pp_structure
    # rapid 後端的 OCR 模型等級/版本/裝置：
    #  mobile + PP-OCRv4 ~4s/頁、結構乾淨(預設)；server + PP-OCRv5 近乎完美但 CPU ~87s/頁(GPU 才實用)。
    OCR_MODEL_TIER: str = "mobile"              # mobile | server
    OCR_VERSION: str = "PP-OCRv4"               # PP-OCRv4 | PP-OCRv5
    OCR_DEVICE: str = "cpu"                     # cpu | gpu(需 onnxruntime-gpu;正式機 5090 用)

    # 混合檢索（向量 + BM25 關鍵字，用 RRF 融合）。對「靠關鍵字才找得到的分散子項目」recall 大幅提升。
    # 僅 SQLite 生效（FTS5 trigram）；非 SQLite 自動退回純向量。
    # 表格逐列增強：只對最相關的前 N 個來源把表格序列化成「逐列 key=value」(幫小模型查 cell)；
    # 全部都加會讓 context 加倍而撐爆 num_ctx → 生成退化，故預設只增強最前面 1 個。
    RAG_TABLE_ROWWISE_TOP: int = 1
    RAG_HYBRID_SEARCH: bool = True
    RAG_RRF_K: int = 60                         # RRF 常數，越大越平滑（標準值 60）
    # 中文查詢救援：語料是英文規範、使用者用中文問時，FTS5 trigram 會把整句中文當成
    # 單一片語，實測 12 題全部 0 命中 —— 混合檢索的關鍵字那一半等於空轉。
    # 開啟後「只在 BM25 撈到 0 筆且查詢含中文」時，才用 LLM 轉成英文檢索詞重試一次。
    # 設計成「失效才觸發」而非全域開關：BM25 本來就有命中的情況（中文語料、英文查詢、
    # 含料號/條號的查詢）完全不會進來，不付額外延遲、也不會影響原本正常的排序。
    # 實測（12 題 / MIL-STD-810H）：recall@5 0.567→0.667，翻譯耗時約 0.44s（模型已載入）。
    RAG_QUERY_TRANSLATE_FALLBACK: bool = True
    # 翻譯逾時（秒）；逾時直接放棄救援、不拖累查詢。
    # 設 8 秒會在「模型是冷的」時踩到（實測 qwen3:8b 光載入就約 6 秒），
    # 白白放棄本來有效的救援。逾時只是放棄、無其他副作用，故放寬到 15 秒。
    RAG_QUERY_TRANSLATE_TIMEOUT: int = 15
    # 融合後餵進 LLM 的「context 塊數」上限。hybrid recall 高但塊一多就破碎，
    # 過多分散塊會讓生成退化；超出的命中仍會列在 sources（前端可預覽），只是不進 LLM context。
    RAG_MAX_CONTEXT_CHUNKS: int = 6

    # 撈寬再精選（rerank）：對融合後的候選池，用一次便宜 LLM 打分，把「真正含規格/判定/程序」
    # 的乾淨段落排到前面、把修訂紀錄/續頁/目錄等垃圾踢掉，再餵生成。失敗自動退回原順序。
    RAG_RERANK: bool = True
    # 送進 rerank 的候選數。
    #
    # 一度依外部審查建議加大到 24（該報告以自建 10 題集量到 7/10 → 8/10），但
    # 本專案的 golden set（30 題）顯示加大反而是淨損失，而且更慢：
    #     snippet 1800 / pool 12   hit@5 0.733  MRR 0.549  146s   <- 最佳
    #     snippet 1800 / pool 24   hit@5 0.733  MRR 0.516  179s
    #     snippet 1800 / pool 40   hit@5 0.667  MRR 0.501  244s
    # 候選越多，reranker 要在更多雜訊裡挑，排序反而變差。
    #
    # 真正決定成敗的是 snippet 長度而非 pool 大小：
    #     snippet 500  / pool 24   hit@5 0.433  MRR 0.300   <- 少 9 題
    # reranker 先前不是弱，是被餓著（平均塊長 1465，卻只餵它 500 字元）。
    RAG_RERANK_POOL: int = 12
    RAG_RERANK_MIN_SCORE: int = 3               # LLM 後端：低於此分（0-10）視為不相關，剔除
    # rerank 後端：cross_encoder（專門模型，CPU，~1-3s，建議）／ llm（用 gemma 打分，慢 ~100s）。
    # cross_encoder 載入失敗會自動退回 llm。
    RAG_RERANK_BACKEND: str = "cross_encoder"
    RAG_RERANK_MODEL: str = "BAAI/bge-reranker-base"  # 多語(含中英)cross-encoder，CPU 可跑
    # rerank 送進 cross-encoder 的每塊字元數。原本寫死 500，而語料平均塊長 1465
    # —— reranker 對每塊後 2/3 沒看到就打分，規範的數值常在後半。與切塊 max_chars 對齊。
    # Qwen3-Embedding 是非對稱模型：查詢端加 instruct 前綴、文件端不加。
    # 只影響查詢，不需重建索引；設 False 可立即回到舊行為做 A/B。
    EMBEDDING_QUERY_INSTRUCT: bool = True
    RAG_RERANK_SNIPPET_CHARS: int = 1800
    # auto = 有 CUDA 就用 GPU。原本寫死 cpu，rerank 是每次查詢都跑的階段，顯卡卻閒置。
    RAG_RERANK_DEVICE: str = "auto"      # auto | cpu | cuda
    # 融合第 1 名保底置頂 —— 預設關閉，因為實測是淨損失。
    #
    # 它的原意是「rerank 對數字表／跨語言評分不準，可能把融合第 1 名踢出
    # top_n，所以保底放回並置頂」。但 golden set 量測顯示它一直在扯 reranker
    # 後腿（30 題）：
    #     完全不 rerank            hit@5 0.633  MRR 0.472
    #     rerank + 保底（舊預設）   hit@5 0.700  MRR 0.447
    #     rerank，關掉保底          hit@5 0.733  MRR 0.516
    #
    # 開著保底時，reranker 看起來「多拉進 2 題卻把原有的往下推」，像是隨機重排；
    # 關掉之後才看得出它真正的貢獻（比不 rerank 多 3 題，且 MRR 同步上升）。
    # 原因是它會把 cross-encoder 判為不相關的塊硬塞到位置 0（審查實測有一例
    # CE 分數 0.172 vs 最佳 0.998），還連帶污染 primary_page 的頁距視窗。
    RAG_RERANK_GUARANTEE_TOP: bool = False

    # 低信心 fallback：以「最相關段落的 cross-encoder 分數」當信心訊號。實測相關題 top 分數
    # ≥0.4(離題 ≈0.00），故門檻設 0.15 可乾淨分開。低於此值時不硬答，改回「最接近的內容
    # ＋ LLM 動態產生的查詢修正建議」。CE 不可用時此 gate 自動略過（不退化）。
    RAG_LOWCONF_CE_THRESHOLD: float = 0.15
    RAG_RERANK_CE_MIN_SCORE: float = 0.0        # cross-encoder 分數門檻（logit，>0 偏相關）



    @field_validator("OLLAMA_KEEP_ALIVE", mode="before")
    @classmethod
    def _normalize_keep_alive(cls, v):
        # Ollama daemon 接受純數字（"-1" = 永久），但 Ollama API 要求帶單位（如 "5m"）。
        # 系統環境常為 daemon 設 OLLAMA_KEEP_ALIVE=-1，會被 pydantic-settings 一起讀進來。
        # 這裡若拿到純數字 / 負數，補上秒單位避免送 API 時 400。
        if v is None:
            return v
        s = str(v).strip()
        if not s:
            return s
        # 已帶單位（s/m/h）就直接用
        if s[-1].lower() in {"s", "m", "h"}:
            return s
        # 純數字（含負號）→ 補 's'
        try:
            int(s)
            return f"{s}s"
        except ValueError:
            return s

    class Config:
        env_file = ".env"


settings = Settings()
