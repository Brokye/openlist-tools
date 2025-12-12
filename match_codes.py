import requests
from bs4 import BeautifulSoup
import csv
import time
import re
import json
import random
import os
import unicodedata
from difflib import SequenceMatcher

# =================配置部分=================
INPUT_FILE = 'd_code.txt'
OUTPUT_FILE = 'result.csv'
MIN_SIMILARITY = 0.65  # 建议阈值提高到 0.65 (因为清洗后匹配度会变高)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Referer': 'https://www.dmm.co.jp/'
}
DMM_COOKIES = {'age_check_done': '1', 'ckcy': '1'}


# ==========================================

class TitleMatcher:
    """
    专门用于处理日文同人音声标题匹配的工具类
    """

    def __init__(self):
        # 1. 标签正则：匹配 【...】 或 [...]
        self.pattern_tags = re.compile(r'【.*?】|\[.*?\]|\(.*?\)')
        # 2. 噪声正则：匹配非文字符号
        self.pattern_noise = re.compile(r'[\s　~～\-\:：×\.…!！\?？○●◎★☆◆◇■□△▲▽▼※＊*]')

    def normalize(self, text):
        """核心清洗逻辑"""
        if not text: return ""

        # Step 1: NFKC 标准化 (全角转半角)
        text = unicodedata.normalize('NFKC', text)

        # Step 2: 移除标签 (【耳かき】等)
        text = self.pattern_tags.sub('', text)

        # Step 3: 移除通用噪声符号
        text = self.pattern_noise.sub('', text)

        # Step 4: 日语异形词修正 (关键!)
        # 将 DMM 习惯的 "癒やし" 统一为 DLsite 习惯的 "癒し"
        text = text.replace('癒やし', '癒し')

        # Step 5: 移除助词 (可选，减少语法差异)
        text = re.sub(r'[をがの]', '', text)

        return text.lower()

    def get_similarity(self, str1, str2):
        """计算清洗后的相似度"""
        norm1 = self.normalize(str1)
        norm2 = self.normalize(str2)
        if not norm1 or not norm2: return 0.0
        return SequenceMatcher(None, norm1, norm2).ratio()


def get_dmm_title(d_code):
    """从 DMM 获取标题"""
    # 注意：这里使用的是搜索页，为了准确性，建议确认 searchstr 是否只返回唯一结果
    url = f"https://www.dmm.co.jp/search/=/searchstr={d_code}/limit=30/sort=rankprofile"
    try:
        response = requests.get(url, headers=HEADERS, cookies=DMM_COOKIES, timeout=15)
        if response.status_code != 200: return None
        soup = BeautifulSoup(response.text, 'html.parser')

        # 尝试适配两种常见的 DMM 列表结构
        title_tag = soup.find('p', class_="text-sm font-bold line-clamp-2")  # 现代样式
        if not title_tag:
            # 备用选择器 (列表样式)
            title_tag = soup.find('span', class_="txt")

        if title_tag:
            return title_tag.get_text(strip=True)
        return None
    except Exception as e:
        print(f"DMM 请求错误: {e}")
        return None


def generate_search_candidates(raw_title):
    """
    生成搜索关键词列表。
    策略：
    1. 清洗后的全名 (最准)
    2. 移除特定伏字后的名称
    """
    candidates = []

    # 基础清洗：移除开头结尾的空格
    base_title = raw_title.strip()

    # 策略 1: 修复常见的伏字 (DMM 经常把 '奴隷' 写成 '奴●')
    fixed_title = base_title
    fixed_title = re.sub(r'奴[●○]', '奴隷', fixed_title)
    fixed_title = re.sub(r'調[●○]', '調教', fixed_title)
    fixed_title = re.sub(r'レ[●○×]プ', 'レイプ', fixed_title)

    # 移除常见的干扰符号，生成纯净标题作为搜索词
    clean_search = re.sub(r'[○●◎★☆◆◇■□△▲▽▼※×＊*]', ' ', fixed_title)
    clean_search = re.sub(r'\s+', ' ', clean_search).strip()  # 合并空格

    candidates.append(clean_search)

    # 策略 2: 如果标题非常长，尝试截取前半部分 (DLsite 搜索有时候对过长关键词支持不好)
    # 截取直到遇到第一个特殊符号或空格，长度至少要 5
    if len(clean_search) > 10:
        short_search = clean_search[:15]
        if short_search not in candidates:
            candidates.append(short_search)

    return candidates


def get_dlsite_candidates_list(search_term):
    """调用 DLsite Suggest API 获取候选列表"""
    if len(search_term) < 2: return []

    base_url = "https://www.dlsite.com/suggest/?"
    timestamp = int(time.time() * 1000)
    callback_name = f"jQuery{random.randint(10 ** 19, 10 ** 20 - 1)}_{timestamp}"
    params = {
        'callback': callback_name,
        'term': search_term,
        'site': 'adult-jp',
        'time': timestamp,
        '_': timestamp + 5
    }

    try:
        response = requests.get(base_url, params=params, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            match = re.search(r'^\s*.*?\(({.*})\);\s*$', response.text, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                return data.get('work', [])
    except Exception as e:
        print(f"DLsite API 错误: {e}")
    return []


def main():
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 错误: 找不到 {INPUT_FILE}")
        return

    # 初始化匹配器
    matcher = TitleMatcher()

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        d_codes = [line.strip() for line in f if line.strip()]

    print(f"🚀 开始处理 {len(d_codes)} 个条目 (集成智能清洗版)...")

    with open(OUTPUT_FILE, 'w', encoding='utf-8-sig', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['DMM原名', 'DLSite匹配标题', 'd_code', 'RJ_code', '相似度', '状态'])

        for idx, d_code in enumerate(d_codes):
            print(f"\n[{idx + 1}/{len(d_codes)}] 正在搜索: {d_code}")

            dmm_title = get_dmm_title(d_code)

            # 全局最佳结果容器
            best_match = {
                "rj": "Not Found",
                "title": "",
                "score": 0.0,
                "status": "未找到"
            }

            if dmm_title:
                print(f"    📝 DMM标题: {dmm_title[:40]}...")

                search_terms = generate_search_candidates(dmm_title)

                # 已检查过的 RJ 号集合，避免重复计算
                checked_rjs = set()

                for term in search_terms:
                    # 如果已经找到了极高相似度 (>0.9)，跳过后续搜索词
                    if best_match["score"] > 0.9:
                        break

                    candidates_list = get_dlsite_candidates_list(term)

                    if candidates_list:
                        # 遍历该搜索词返回的所有结果
                        for item in candidates_list:
                            dl_rj = item.get('workno')
                            dl_title = item.get('work_name')

                            if dl_rj in checked_rjs: continue
                            checked_rjs.add(dl_rj)

                            # === 核心：使用清洗后的相似度计算 ===
                            sim = matcher.get_similarity(dmm_title, dl_title)

                            # 调试日志 (可选)
                            # if sim > 0.5:
                            #     print(f"       候选: {dl_rj} | 分数: {sim:.2f} | {dl_title[:15]}...")

                            if sim > best_match["score"]:
                                best_match["score"] = sim
                                best_match["rj"] = dl_rj
                                best_match["title"] = dl_title
                                best_match["status"] = "成功"

                    time.sleep(random.uniform(0.5, 1.0))  # 随机延迟

                # 最终判定
                if best_match["rj"] != "Not Found":
                    print(
                        f"    ✅ 最终选中: {best_match['rj']} | 相似度: {best_match['score']:.2f} | {best_match['title'][:20]}...")

                    if best_match["score"] < MIN_SIMILARITY:
                        best_match["status"] = "相似度过低"
                        print(f"    ⚠️ 警告: 相似度低于阈值 ({MIN_SIMILARITY})")
                else:
                    print("    ❌ 未找到任何匹配")

            else:
                print("    ⚠️ DMM标题获取失败")
                best_match["status"] = "DMM Error"
                dmm_title = "Error"

            # 写入 CSV
            writer.writerow([
                dmm_title,
                best_match["title"],
                d_code,
                best_match["rj"],
                f"{best_match['score']:.2f}",
                best_match["status"]
            ])

            # 这里的 sleep 是为了防止 DMM 封 IP
            time.sleep(random.uniform(1.0, 2.0))

    print(f"\n🎉 处理完成，结果已保存至 {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
