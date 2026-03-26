import os
import re
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv
import streamlit as st
import dropbox

from sp_api.api import CatalogItems, Products
from sp_api.base import Marketplaces, SellingApiException

from config import (
    COUNTRY_LABEL_TO_CODE,
    get_fx_rate,
    get_fee_rate,
    country_to_lang,
)

from google import genai  # Gemini SDK

# ---------------------------------------------------------
# 設定
# ---------------------------------------------------------

load_dotenv()


def env(name: str) -> str:
    """ローカル(.env)優先、なければ Streamlit secrets。"""
    v = os.environ.get(name)
    if v is not None:
        return v
    return st.secrets[name]


gemini_client = genai.Client(api_key=env("GEMINI_API_KEY"))


def get_dropbox_client():
    return dropbox.Dropbox(
        oauth2_refresh_token=env("DROPBOX_REFRESH_TOKEN"),
        app_key=env("DROPBOX_APP_KEY"),
        app_secret=env("DROPBOX_APP_SECRET"),
    )


def get_credentials():
    return dict(
        refresh_token=env("SP_API_REFRESH_TOKEN"),
        lwa_app_id=env("LWA_CLIENT_ID"),
        lwa_client_secret=env("LWA_CLIENT_SECRET"),
        aws_access_key=env("AWS_ACCESS_KEY"),
        aws_secret_key=env("AWS_SECRET_KEY"),
        role_arn=env("ROLE_ARN"),
    )


# ---------------------------------------------------------
# Amazon 商品取得系
# ---------------------------------------------------------

def extract_asin(text: str) -> str:
    """URLでも生ASINでも、10桁のASINだけ抜き出す"""
    if not text:
        return ""
    text = text.strip().upper()

    m = re.search(r"/([A-Z0-9]{10})(?:[/?]|$)", text)
    if m:
        return m.group(1)

    if re.fullmatch(r"[A-Z0-9]{10}", text):
        return text

    return ""


def extract_jp_text_list(attr: dict, key: str) -> List[str]:
    """attributes[key] から language_tag==ja_JP の value をリストで返す。"""
    values: List[str] = []
    items = attr.get(key) or []
    for v in items:
        if v.get("language_tag") == "ja_JP" and "value" in v:
            values.append(v["value"])
    return values


def fetch_amazon_item(asin: str) -> dict:
    """
    仕様書1.3で必要な項目だけ取得。
    - title, image_urls, price_jpy, jp_description, raw_attributes [file:468]
    """
    credentials = get_credentials()

    # CatalogItems
    try:
        catalog_client = CatalogItems(
            marketplace=Marketplaces.JP,
            credentials=credentials,
        )
        res = catalog_client.get_catalog_item(
            asin=asin,
            marketplaceIds=[Marketplaces.JP.marketplace_id],
            includedData=["summaries", "attributes", "images"],
        )
    except SellingApiException as e:
        return {"asin": asin, "error": f"CatalogItems エラー: {e}", "detail": str(e)}

    payload = res.payload or {}
    summaries = payload.get("summaries", [])
    images_per_marketplace = payload.get("images", [])
    attributes = payload.get("attributes", {})

    summary = summaries[0] if summaries else {}
    title = summary.get("itemName") or "取得できませんでした"

    # 画像URL
    image_urls: List[str] = []
    for marketplace_images in images_per_marketplace:
        if marketplace_images.get("marketplaceId") != Marketplaces.JP.marketplace_id:
            continue
        for img in marketplace_images.get("images", []):
            link = img.get("link")
            if link:
                image_urls.append(link)
        break

    # 日本語商品説明:
    # 1. product_description（今回欲しかった長文）
    # 2. bullet_point
    # 3. safety_warning
    product_desc = extract_jp_text_list(attributes, "product_description")
    bullet_texts = extract_jp_text_list(attributes, "bullet_point")
    safety_texts = extract_jp_text_list(attributes, "safety_warning")

    jp_description_parts = product_desc + bullet_texts + safety_texts
    jp_description = "\n".join(jp_description_parts)

    # 価格（BuyBox）
    price_jpy = None
    price_error = None
    try:
        pricing_client = Products(
            marketplace=Marketplaces.JP,
            credentials=credentials,
        )
        price_res = pricing_client.get_item_offers(
            asin=asin,
            item_condition="New",
        )
        price_payload = price_res.payload or {}
        summary_data = price_payload.get("Summary", {})
        buy_box = summary_data.get("BuyBoxPrices", [])

        if buy_box:
            lp = buy_box[0].get("ListingPrice", {})
            amount = lp.get("Amount")
            currency = lp.get("CurrencyCode")
            if amount is not None and currency == "JPY":
                price_jpy = float(amount)
    except SellingApiException as e:
        price_jpy = None
        price_error = str(e)

    return {
        "asin": asin,
        "title": title,
        "image_urls": image_urls,
        "price_jpy": price_jpy,
        "raw_attributes": attributes,
        "price_error": price_error,
        "jp_description": jp_description,
    }


# ---------------------------------------------------------
# 利益計算
# ---------------------------------------------------------

def calc_price_and_profit(country_code: str, jp_price: float, shipping_jpy: float, target_margin: float):
    fee = get_fee_rate(country_code)
    cost = jp_price + shipping_jpy
    denom = 1.0 - fee - target_margin
    if denom <= 0:
        return 0, 0
    selling_jpy = cost / denom
    profit_jpy = selling_jpy - cost
    return round(selling_jpy), round(profit_jpy)


# ---------------------------------------------------------
# Dropbox 画像保存
# ---------------------------------------------------------

def save_images_to_dropbox(
    dbx: dropbox.Dropbox,
    image_urls: List[str],
    asin: str,
    title: str,
    base_folder: str,
):
    from urllib.parse import urlparse

    folder = f"/{base_folder.strip('/')}/{asin}"

    grouped: Dict[str, List[str]] = {}
    for url in image_urls:
        if not url:
            continue
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or f"{asin}.jpg"
        if "SL75" in filename:
            continue
        grouped.setdefault(filename, []).append(url)

    saved_paths: List[str] = []

    for filename, urls in grouped.items():
        url = urls[0]
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.content

        dropbox_path = f"{folder}/{filename}"
        dbx.files_upload(
            data,
            dropbox_path,
            mode=dropbox.files.WriteMode("overwrite"),
        )
        saved_paths.append(dropbox_path)

    return saved_paths


# ---------------------------------------------------------
# 翻訳テキスト保存 & Gemini
# ---------------------------------------------------------

def save_translation_to_dropbox(
    dbx: dropbox.Dropbox,
    asin: str,
    jp_title: str,
    cost_price_jpy: int | float,
    selling_local_str: str,
    selling_jpy: int | float,
    profit_jpy: int | float,
    translated_title: str,
    translated_description: str,
    base_folder: str,
) -> str:
    folder = f"/{base_folder.strip('/')}/{asin}"

    safe_title_head = jp_title[:10].replace("/", "_").replace("\\", "_")
    filename = f"{asin}_{safe_title_head}.txt"
    dropbox_path = f"{folder}/{filename}"

    lines = [
        jp_title,
        f"仕入れ値: {int(cost_price_jpy)}円",
        f"販売価格: {selling_local_str}（{int(selling_jpy)}円）",
        f"利益額: {int(profit_jpy)}円",
        "--------------------",
        translated_title,
        "--------------------",
        translated_description,
    ]
    data = "\n".join(lines).encode("utf-8")

    dbx.files_upload(
        data,
        dropbox_path,
        mode=dropbox.files.WriteMode("overwrite"),
    )
    return dropbox_path


def call_gemini_api(prompt: str) -> str:
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text or ""


def translate_with_gemini(
    jp_title: str,
    jp_description: str,
    country: str,
) -> Tuple[str, str]:
    target_lang = country_to_lang(country)

    prompt = f"""
以下の日本語のEC商品タイトルと商品説明を{target_lang}に翻訳してください。
Shopeeの商品ページ向けなので、自然で読みやすい文に整えてください。
最初に翻訳タイトル、その次に空行を1つ挟んで、翻訳された商品説明を出力してください。

[タイトル]
{jp_title}

[商品説明]
{jp_description}
"""

    response_text = call_gemini_api(prompt)

    lines = response_text.strip().splitlines()
    translated_title = lines[0].strip() if lines else ""
    translated_description = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""

    return translated_title, translated_description


# ---------------------------------------------------------
# メイン画面
# ---------------------------------------------------------

def main():
    st.set_page_config(page_title="ASIN2Shopee", layout="wide")

    st.title("ASIN2Shopee（Amazon → Shopee 商品変換ツール）")

    country_label = st.selectbox(
        "国",
        options=list(COUNTRY_LABEL_TO_CODE.keys()),
        index=0,
    )
    country_code = COUNTRY_LABEL_TO_CODE[country_label]

    asin_or_url = st.text_input("ASIN / Amazon URL", value="")

    shipping_fee_str = st.text_input("送料（円）", value="500", placeholder="例: 500")

    try:
        shipping_fee = int(shipping_fee_str.replace(",", "").strip() or "0")
        if shipping_fee < 0:
            shipping_fee = 0
    except ValueError:
        shipping_fee = 0

    margin_options = list(range(5, 95, 5))
    target_margin_pct = st.selectbox(
        "目標利益率（%）",
        options=margin_options,
        index=margin_options.index(20),
        format_func=lambda x: f"{x}%",
    )
    target_margin = target_margin_pct / 100.0

    st.markdown("---")

    get_clicked = st.button("取得")
    result_container = st.container()
    save_clicked = st.button("保存")
    error_placeholder = st.empty()

    # ---- 取得 ----
    if get_clicked:
        asin = extract_asin(asin_or_url)
        if not asin:
            error_placeholder.error("ASINまたは有効なAmazon商品URLを入力してください。")
            return

        with st.spinner("Amazon商品情報を取得中..."):
            item = fetch_amazon_item(asin)

        if item is None or "error" in item:
            msg = item.get("error") if item else "不明なエラー"
            detail = item.get("detail")
            if detail:
                error_placeholder.error(f"{msg}\n詳細: {detail}")
            else:
                error_placeholder.error(f"Amazon商品情報の取得に失敗しました: {msg}")
            return

        with result_container:
            st.subheader("Amazon商品情報")
            st.write(item["title"])
            st.write(f"ASIN: {item['asin']}")

            if item.get("image_urls"):
                st.image(item["image_urls"][0], caption="", width=200)
            else:
                st.write("画像なし")

            st.markdown("### attributes 生データ（調査用）")
            st.json(item.get("raw_attributes", {}))

            st.markdown("### 日本語商品説明（抽出結果・確認用）")
            st.text_area(
                "JP Description (readonly)",
                item.get("jp_description", ""),
                height=200,
                disabled=True,
            )

            st.markdown("---")
            st.subheader("利益計算結果")

            cost_price = int(item["price_jpy"]) if item.get("price_jpy") else 0

            if shipping_fee <= 0:
                st.write("販売価格 / 利益額: N/A（送料未入力）")
                sell_price_rounded = 0
                profit_rounded = 0
            else:
                sell_price_rounded, profit_rounded = calc_price_and_profit(
                    country_code,
                    cost_price,
                    shipping_fee,
                    target_margin,
                )

                if sell_price_rounded == 0:
                    error_placeholder.error(
                        "手数料率と目標利益率の合計が100%未満になるように設定してください。"
                    )

            rate = get_fx_rate(country_code)
            local_currency = {
                "SG": "SGD",
                "MY": "MYR",
                "TH": "THB",
                "PH": "PHP",
                "TW": "TWD",
                "VN": "VND",
                "ID": "IDR",
            }[country_code]
            local_price = round(sell_price_rounded * rate, 2)

            st.write(f"仕入れ値: {cost_price}円")
            st.write(
                f"販売価格: {local_price} {local_currency}"
                f"（{sell_price_rounded}円）"
            )
            st.write(f"利益額: {profit_rounded}円")

        # セッション保存
        st.session_state["last_item"] = item
        st.session_state["last_country_code"] = country_code
        st.session_state["last_shipping_fee"] = shipping_fee
        st.session_state["last_target_margin_pct"] = target_margin_pct
        st.session_state["last_sell_price_jpy"] = sell_price_rounded
        st.session_state["last_local_price"] = local_price
        st.session_state["last_local_currency"] = local_currency
        st.session_state["last_cost_price"] = cost_price
        st.session_state["last_profit_jpy"] = profit_rounded
        st.session_state["last_jp_description"] = item.get("jp_description", "")

    # ---- 保存 ----
    if save_clicked:
        if "last_item" not in st.session_state:
            error_placeholder.warning("先に取得ボタンで商品情報を取得してください。")
        else:
            item = st.session_state["last_item"]
            asin = item["asin"]
            title = item["title"]
            image_urls = item.get("image_urls") or []
            country_code = st.session_state.get("last_country_code")
            sell_price_rounded = st.session_state.get("last_sell_price_jpy", 0)
            local_price = st.session_state.get("last_local_price", 0)
            local_currency = st.session_state.get("last_local_currency", "")
            cost_price = st.session_state.get("last_cost_price", 0)
            profit_rounded = st.session_state.get("last_profit_jpy", 0)
            jp_description = st.session_state.get("last_jp_description", "")

            if not image_urls:
                error_placeholder.error("画像URLがないため、保存できません。")
            else:
                try:
                    dbx = get_dropbox_client()
                    base_folder = os.environ.get("DROPBOX_BASE_FOLDER", "ASIN2Shopee")

                    with st.spinner("画像・翻訳テキストをDropboxに保存中..."):
                        paths = save_images_to_dropbox(
                            dbx=dbx,
                            image_urls=image_urls,
                            asin=asin,
                            title=title,
                            base_folder=base_folder,
                        )

                        translated_title, translated_description = translate_with_gemini(
                            jp_title=title,
                            jp_description=jp_description,
                            country=country_code,
                        )

                        selling_local_str = f"{local_price} {local_currency}"

                        translation_path = save_translation_to_dropbox(
                            dbx=dbx,
                            asin=asin,
                            jp_title=title,
                            cost_price_jpy=cost_price,
                            selling_local_str=selling_local_str,
                            selling_jpy=sell_price_rounded,
                            profit_jpy=profit_rounded,
                            translated_title=translated_title,
                            translated_description=translated_description,
                            base_folder=base_folder,
                        )

                    error_placeholder.info(
                        f"{len(paths)}枚の画像と翻訳テキストを保存しました。"
                    )

                except Exception as e:
                    error_placeholder.error(f"保存処理でエラーが発生しました: {e}")


if __name__ == "__main__":
    main()