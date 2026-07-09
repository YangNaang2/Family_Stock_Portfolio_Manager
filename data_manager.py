import os
import json

DATA_FILE = "family_stocks.json"

def load_data():
    """JSON 파일에서 데이터를 읽어오는 함수"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    else:
        # 파일이 없을 때 초기 데이터
        return {
            "아빠": [],
            "엄마": [],
            "나": []
        }

def save_data(data):
    """데이터를 JSON 파일에 저장하는 함수"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)