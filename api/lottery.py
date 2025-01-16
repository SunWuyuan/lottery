import hashlib
import random
import re
import json
import sys
from flask import Flask, request, jsonify

import dateutil
import requests
import argparse
from dotenv import load_dotenv
import os

load_dotenv()

DRAND_HASH = os.getenv('DRAND_HASH', '52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971')
DRAND_PERIOD = int(os.getenv('DRAND_PERIOD', 3))
DRAND_GENESIS_TIME = int(os.getenv('DRAND_GENESIS_TIME', 1692803367))
BASE_URL = os.getenv('BASE_URL', 'https://linux.do')
CONNECT_URL = os.getenv('CONNECT_URL', 'https://connect.linux.do')
DRAND_SERVER = os.getenv('DRAND_SERVER', 'https://drand.cloudflare.com')

app = Flask(__name__)

DRAND_INFO = {
    "period": DRAND_PERIOD,
    "genesis_time": DRAND_GENESIS_TIME,
    "hash": DRAND_HASH,
}

class LotteryError(Exception):
    """抽奖过程中的基础异常类"""
    pass


class TopicError(LotteryError):
    """主题相关的异常"""
    pass


class ValidationError(LotteryError):
    """数据验证相关的异常"""
    pass


class FileError(LotteryError):
    """文件操作相关的异常"""
    pass


class ForumTopicInfo:
    def __init__(self, topic_id, cookies=None):
        self.topic_id = topic_id
        self.title = None
        self.highest_post_number = None
        self.created_at = None
        self.last_posted_at = None
        self.base_url = BASE_URL
        self.connect_url = CONNECT_URL
        self.cookies = cookies
        self.valid_post_ids = []
        self.valid_post_numbers = []

    @classmethod
    def from_url(cls, url, cookies=None):
        """从URL中解析主题信息"""
        pattern = r"/t/topic/(\d+)(?:/\d+)?"
        match = re.search(pattern, url)
        if not match:
            raise ValidationError("无法从URL中解析出主题ID")

        return cls(match.group(1), cookies)

    """
    def _load_cookies():

        try:
            with open('cookies.txt', 'r') as f:
                content = f.read()
                if len(content) > 0:
                    return {'Cookie': content.strip()}

                return {}
        except FileNotFoundError:
            return {}
    """
    @staticmethod
    def fetch_topic_info(self):
        """获取主题信息"""
        json_url = f"{self.base_url}/t/{self.topic_id}.json"
        try:
            response = requests.get(json_url, headers={'Cookie': self.cookies})
            response.raise_for_status()
            data = response.json()

            # 检查帖子是否已关闭或已存档
            if not (data.get('closed') or data.get('archived')):
                raise ValidationError("帖子尚未关闭或存档，不能进行抽奖")

            if data.get('category_id') not in [36, 60, 61, 62]:
                raise ValidationError("帖子不在指定分类下，不能进行抽奖")

            self.title = data['title']
            self.highest_post_number = data['highest_post_number']
            self.created_at = data['created_at']
            self.last_posted_at = data['last_posted_at']

        except requests.RequestException as e:
            raise TopicError(f"获取主题信息失败: {str(e)}\n如果帖子需要登录，请确保cookies.txt文件存在且内容有效")
        except KeyError:
            raise TopicError("返回的JSON数据格式不正确")

    def fetch_valid_post_numbers(self):
        """获取有效的楼层号"""
        valid_posts_url = f"{self.connect_url}/api/topic/{self.topic_id}/valid_post_number"
        try:
            response = requests.get(valid_posts_url, headers={'Cookie': self.cookies})
            response.raise_for_status()
            data = response.json()

            self.valid_post_numbers = data.get('rows', [])
            if not self.valid_post_numbers:
                raise ValidationError("没有找到有效的楼层")

            self.valid_post_ids = data.get('ids', [])
            if not self.valid_post_ids:
                raise ValidationError("没有找到有效的楼层")

            return self.valid_post_numbers
        except requests.RequestException as e:
            raise TopicError(f"获取有效楼层失败: {str(e)}")
        except (KeyError, ValueError):
            raise TopicError("返回的有效楼层数据格式不正确")

    def get_post_url(self, post_number):
        """获取特定楼层的URL"""
        return f"{self.base_url}/t/topic/{self.topic_id}/{post_number}"


def fetch_drand_randomness(last_posted_at):
    """获取drand随机数"""
    timestamp = int(dateutil.parser.parse(last_posted_at).timestamp())
    round_number = (timestamp - DRAND_INFO['genesis_time']) // DRAND_INFO['period']
    if round_number < 0:
        print("错误: 计算的drand轮次无效")
        sys.exit(1)
    drand_url = f"https://api.drand.sh/{DRAND_INFO['hash']}/public/{round_number}"
    try:
        response = requests.get(drand_url)
        response.raise_for_status()
        data = response.json()
        return data['randomness'], data['round']
    except requests.RequestException as e:
        print(f"错误: 获取云端随机数失败: {str(e)}")
        sys.exit(1)


def generate_final_seed(topic_info, winners_count, use_drand, seed_content):
    """读取seed文件内容并与其他信息一起计算多重哈希值"""
    try:
        if len(seed_content) == 0:
            raise ValidationError("seed内容不能为空")

        md5_hash = hashlib.md5(seed_content).hexdigest()
        sha1_hash = hashlib.sha1(seed_content).hexdigest()
        sha512_hash = hashlib.sha512(seed_content).hexdigest()
        combined = '|'.join([
            md5_hash, sha1_hash, sha512_hash,
            str(winners_count),
            str(topic_info.highest_post_number),
            str(topic_info.topic_id),
            str(topic_info.created_at),
            str(topic_info.last_posted_at),
            ','.join([str(i) for i in topic_info.valid_post_ids]),
            ','.join([str(i) for i in topic_info.valid_post_numbers]),
        ])

        if use_drand:
            drand_randomness, drand_round = fetch_drand_randomness(topic_info.last_posted_at)
            combined += f"|{drand_randomness}|{drand_round}"

        return hashlib.sha256(combined.encode('utf-8')).hexdigest()
    except Exception as e:
        raise FileError(f"读取seed内容时发生错误: {str(e)}")


def generate_winning_floors(seed, valid_floors, winners_count):
    """生成中奖楼层"""
    total_floors = len(valid_floors)
    if winners_count > total_floors:
        raise ValidationError(f"中奖人数({winners_count})不能大于有效楼层数({total_floors})")

    random.seed(seed)
    winning_floors = []
    available_floors = valid_floors.copy()

    for _ in range(winners_count):
        winner = random.choice(available_floors)
        available_floors.remove(winner)
        winning_floors.append(winner)

    return winning_floors


def print_divider(char='=', width=80):
    """打印分隔线"""
    print(char * width)


@app.route('/lottery', methods=['POST'])
def lottery():
    try:
        print("Received POST request")
        if request.content_type == 'application/json':
            data = request.json
            seed_content = data.get('seed', '').encode('utf-8')
            print("Processing JSON data")
        elif request.content_type.startswith('multipart/form-data'):
            data = request.form.to_dict()
            for key in data:
                if isinstance(data[key], list):
                    data[key] = data[key][0]
            if 'seed' in request.files:
                seed_file = request.files['seed']
                seed_content = seed_file.read()
            else:
                seed_content = data.get('seed', '').encode('utf-8')
            print("Processing form-data")
        else:
            print("Unsupported Content-Type")
            return jsonify({'error': 'Unsupported Content-Type'}), 400

        topic_url = data.get('topic_url')
        winners_count = data.get('winners_count')
        if winners_count:
            winners_count = int(winners_count)
        else:
            print("Missing parameter: winners_count")
            raise ValidationError("缺少必要的参数: winners_count")

        use_drand = str(data.get('use_drand', 'false')).lower() in ['true', '1', 'yes', 'y']
        cookies = data.get('cookies', '')

        if not topic_url or not winners_count:
            print("Missing necessary parameters")
            raise ValidationError("缺少必要的参数")

        print(f"Processing lottery for topic URL: {topic_url} with {winners_count} winners")

        topic_info = ForumTopicInfo.from_url(topic_url, cookies)
        topic_info.fetch_topic_info(topic_info)
        valid_floors = topic_info.fetch_valid_post_numbers()

        if len(valid_floors) < 2:
            print("Not enough valid floors")
            raise ValidationError("没有足够的参与楼层")

        final_seed = generate_final_seed(topic_info, winners_count, use_drand, seed_content)
        winning_floors = generate_winning_floors(final_seed, valid_floors, winners_count)

        response = {
            'topic_url': topic_url,
            'title': topic_info.title,
            'created_at': int(dateutil.parser.parse(topic_info.created_at).timestamp()),
            'last_posted_at': int(dateutil.parser.parse(topic_info.last_posted_at).timestamp()),
            'highest_post_number': topic_info.highest_post_number,
            'valid_post_numbers': valid_floors,
            'winners_count': winners_count,
            'final_seed': final_seed,
            'winning_floors': winning_floors,
            'drand_randomness': fetch_drand_randomness(topic_info.last_posted_at) if use_drand else None
        }

        print("Lottery processed successfully")
        return jsonify(response), 200

    except LotteryError as e:
        print(f"Lottery error: {str(e)}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Server error: {str(e)}", exc_info=True)
        return jsonify({'error': '服务器内部错误'}), 500

def load_drand_info(hash_value):
    """加载drand配置信息"""
    drand_url = f"{DRAND_SERVER}/{hash_value}/info"
    try:
        response = requests.get(drand_url)
        response.raise_for_status()
        data = response.json()
        return {
            "period": data['period'],
            "genesis_time": data['genesis_time'],
            "hash": data['hash'],
        }
    except requests.RequestException as e:
        print(f"错误: 获取drand配置信息失败: {str(e)}")
        sys.exit(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='启动服务器并加载drand配置')
    parser.add_argument('--drand-hash', type=str, help='drand哈希值')
    args = parser.parse_args()

    if args.drand_hash:
        DRAND_INFO.update(load_drand_info(args.drand_hash))

    print(f"Loaded DRAND_INFO: {DRAND_INFO}")
    print("Starting server on port 3000")
    app.run(host='0.0.0.0', port=3000)