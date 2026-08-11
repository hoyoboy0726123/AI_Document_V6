import os
import sys
import uvicorn
import webbrowser
from multiprocessing import freeze_support
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.main import app as api_app

# 核心路徑雙軌制 (2026 安全版)
if getattr(sys, "frozen", False):
    # 打包後路徑：sys._MEIPASS (內部唯讀資源)
    base_dir = sys._MEIPASS
    # 執行檔實體所在目錄 (外部讀寫資料)
    external_dir = os.path.dirname(sys.executable)
    
    # 強制清理 sys.path 防範劫持
    sys.path = [base_dir] + [p for p in sys.path if base_dir in p]
    
    # 環境變數覆寫 (確保資料寫在外部)
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(external_dir, 'doc_management.db')}"
    os.environ["FILE_STORAGE_DIR"] = os.path.join(external_dir, "storage")
    os.environ["PDF_STORAGE_DIR"] = os.path.join(external_dir, "storage", "documents")
    os.environ["PDF_TEMP_DIR"] = os.path.join(external_dir, "storage", "tmp")
    os.environ["FAISS_INDEX_PATH"] = os.path.join(external_dir, "storage", "faiss_index.bin")
    
    # 確保存儲目錄存在
    os.makedirs(os.path.join(external_dir, "storage", "documents"), exist_ok=True)
    os.makedirs(os.path.join(external_dir, "storage", "tmp"), exist_ok=True)
    os.makedirs(os.path.join(external_dir, "logs"), exist_ok=True)
else:
    # 開發環境路徑
    base_dir = os.path.dirname(os.path.abspath(__file__))

# 靜態資源路徑 (打包後在 sys._MEIPASS 下)
static_dir = os.path.join(base_dir, "frontend_dist")

# 合併 API 與 前端資源 (修正路徑重複問題)
# 直接使用從 app.main 匯入的 app，它已經包含了 /api/v1 路由
app = api_app

# 移除原始 main.py 中的根目錄路由，避免它擋住前端 index.html
# 使用原地修改 (In-place modification) 因為 app.routes 可能沒有 setter
routes_to_keep = [r for r in app.routes if getattr(r, "path", None) != "/"]
app.routes[:] = routes_to_keep

# SPA fallback：React Router 的深層路由（/documents、/qa…）在磁碟上沒有對應檔案，
# 原本的 StaticFiles 會直接回 404 —— 使用者一按重新整理或用書籤進站就壞掉。
# 只對「沒有副檔名」的路徑回退到 index.html，讓前端路由自己接手；
# 缺少的 .js/.css/.png 仍然正常回 404，不會被 index.html 蓋掉而難以除錯。
# 注意：StaticFiles 找不到檔案時是「丟出 HTTPException」而非回傳 404 response，
# 所以這裡必須攔例外，判斷狀態碼是攔不到的。
class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not os.path.splitext(path)[1]:
                return await super().get_response("index.html", scope)
            raise


# 掛載前端靜態檔案到根目錄
if os.path.exists(static_dir):
    app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="static")
else:
    @app.get("/")
    def missing_frontend():
        return {"error": "Frontend dist not found at " + static_dir}

def start_server():
    try:
        print("[Standalone] Starting server at http://127.0.0.1:8000")
        
        # 自動開啟瀏覽器
        webbrowser.open("http://127.0.0.1:8000")
        
        # 啟動 Uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
    except Exception as e:
        import traceback
        print("\n" + "="*50)
        print("CRITICAL ERROR DURING STARTUP:")
        traceback.print_exc()
        print("="*50)
        input("\nPress Enter to exit...")

if __name__ == "__main__":
    freeze_support()
    start_server()
