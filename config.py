# config.py

# --- 国名（表示用） ---
COUNTRY_NAMES: dict[str, str] = {
    "SG": "シンガポール",
    "MY": "マレーシア",
    "TH": "タイ",
    "VN": "ベトナム",
    "PH": "フィリピン",
    "TW": "台湾",
    "ID": "インドネシア",
}

# Streamlit プルダウン用ラベル→コード
COUNTRY_LABEL_TO_CODE: dict[str, str] = {
    v: k for k, v in COUNTRY_NAMES.items()
}

# --- 翻訳ターゲット言語（ここだけいじれば変更できる） ---
COUNTRY_TO_LANG: dict[str, str] = {
    "SG": "英語",
    "MY": "英語",
    "TH": "タイ語",
    "VN": "ベトナム語",
    "PH": "英語",
    "TW": "中国語（繁体字）",
    "ID": "インドネシア語",
}

def country_to_lang(country: str) -> str:
    return COUNTRY_TO_LANG.get(country, "英語")


# --- 手数料率（仕様書 v1.3 初期値） ---
FEES_RATE: dict[str, float] = {
    "PH": 0.09,      # フィリピン
    "TW": 0.09,      # 台湾
    "DEFAULT": 0.10, # それ以外
}

def get_fee_rate(country: str) -> float:
    return FEES_RATE.get(country, FEES_RATE["DEFAULT"])


# --- 固定為替レート（仕様書 v1.3 初期値） ---
FX_RATE_JPY_TO_LOCAL: dict[str, float] = {
    "SG": 0.0081,  # SGD/JPY
    "MY": 0.0250,  # MYR/JPY
    "TH": 0.207,   # THB/JPY
    "PH": 0.376,   # PHP/JPY
    "TW": 0.200,   # TWD/JPY
    "VN": 165.2,   # VND/JPY
    "ID": 106.5,   # IDR/JPY
}

def get_fx_rate(country: str) -> float:
    return FX_RATE_JPY_TO_LOCAL[country]