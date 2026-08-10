# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

# 基礎路徑設定
base_path = os.path.abspath(".")
backend_path = os.path.join(base_path, "backend")

# 1. 包含前端 'frontend_dist' 資料夾 (將被打包至 sys._MEIPASS/frontend_dist)
datas = [
    (os.path.join(backend_path, 'frontend_dist'), 'frontend_dist'),
    (os.path.join(backend_path, '.env_example'), '.'), # 提供 .env 範本
]

# OpenCC 的轉換字典是資料檔，不是 Python 模組 —— 只加 hiddenimports 不夠，
# 漏了它凍結版會靜默退回內建的 90 字對照表（不會 crash，但簡繁轉換幾乎失效）。
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files('opencc')
except Exception:
    pass

# 2. 隱藏導入 (針對 FastAPI, Uvicorn, SQLAlchemy 等)
# 註：RAG rerank 的 cross-encoder 需要 torch + sentence-transformers + transformers。
# 若要在 .exe 內啟用 cross-encoder，需另外 collect_all('sentence_transformers')/
# ('transformers') 並打包模型權重；否則 rerank 會自動降級為 llm 後端（不會 crash）。
hiddenimports = [
    'uvicorn.logging', 
    'uvicorn.loops.auto', 
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto', 
    'uvicorn.lifespan.on',
    'fastapi', 
    'sqlalchemy.sql.default_comparator', 
    'pydantic',
    'pydantic_settings',
    'passlib.handlers.bcrypt',
    'bcrypt',
    'google.genai', 
    'pandas', 
    'openpyxl',
    'email.mime.text', 
    'email.mime.multipart', 
    'email.mime.application',
    'opencc',
    'app.main',
    'app.models',
    'app.database',
    'app.api.v1.admin',
    'app.api.v1.auth',
    'app.api.v1.documents',
    'app.api.v1.folders',
    'app.api.v1.metadata',
    'app.api.v1.rag',
    'app.api.v1.tasks',
    'app.api.v1.vector_search',
    'app.api.v1.kg',
    'app.api.v1.agent',
    'app.services.users',
    'app.services.kg_extractor',
    'app.services.kg_relations',
    'app.services.kg_pipeline',
    'app.services.kg_service',
    'app.services.agent',
    'app.services.agent_tools',
    'app.services.hybrid_search',
    'app.services.rerank',
    'app.services.llm_provider',
    'app.services.llm_provider.base',
    'app.services.llm_provider.ollama_provider',
    'app.services.llm_provider.gemini_provider',
    'app.core.config',
    'app.utils.logging_config'
]

a = Analysis(
    [os.path.join(backend_path, 'standalone_launcher.py')],
    pathex=[backend_path],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AI_Document_V3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True, # 設為 True 以便排錯，穩定後可改為 False
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
