# -*- coding: utf-8 -*-
"""
实现搜书吧论坛登入和发布空间动态
"""
import os
import re
import sys
from copy import copy

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from resilience import TaskError, read_page, send_once, text, write_summary
import time
import logging
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

ch = logging.StreamHandler(stream=sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)
network_logger = logging.getLogger('resilience')
network_logger.setLevel(logging.INFO)
network_logger.addHandler(ch)

def get_refresh_url(url: str):
    def parse(response):
        soup = BeautifulSoup(text(response), 'html.parser')
        for tag in soup.find_all('meta'):
            if str(tag.get('http-equiv', '')).lower() == 'refresh':
                match = re.search(r'url\s*=\s*(.+)', tag.get('content', ''), re.I)
                if match:
                    target = urljoin(response.url, match.group(1).strip().strip('\"\x27'))
                    if urlparse(target).scheme in ('http', 'https') and urlparse(target).hostname:
                        return target
        raise TaskError('解析入口跳转', '页面缺少 meta refresh 跳转字段')
    return read_page(requests, url, '读取入口跳转页', parse, allow_redirect_document=True)

def get_url(url: str):
    def parse(response):
        soup = BeautifulSoup(text(response), 'html.parser')
        for link in soup.find_all('a', href=True):
            if link.get_text(strip=True) == '搜书吧':
                target = urljoin(response.url, link['href'])
                if urlparse(target).scheme in ('http', 'https') and urlparse(target).hostname:
                    return target
        raise TaskError('解析论坛地址', '入口页面缺少有效的搜书吧链接')
    return read_page(requests, url, '读取论坛地址页', parse)


def form_fields(response, login=False):
    soup = BeautifulSoup(text(response), 'html.parser')
    token = soup.find('input', attrs={'name': 'formhash'})
    stage = '解析登录页' if login else '解析空间页'
    if not token or not token.get('value'):
        raise TaskError(stage, '页面缺少 formhash，可能是异常页面或格式变化')
    if not login:
        return token['value']
    container = soup.find(id=re.compile(r'^main_messaqge_.+'))
    if not container:
        raise TaskError(stage, '页面缺少 loginhash，可能是异常页面或格式变化')
    return container['id'].removeprefix('main_messaqge_'), token['value']

class SouShuBaClient:

    def __init__(self, hostname: str, username: str, password: str, questionid: str = '0', answer: str = None,
                 proxies: dict | None = None):
        self.session: requests.Session = requests.Session()
        self.hostname = hostname
        self.username = username
        self.password = password
        self.questionid = questionid
        self.answer = answer
        self._common_headers = {
            "Host": f"{ hostname }",
            "Connection": "keep-alive",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,cn;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        self.proxies = proxies
        self.completed = 0

    def login_form_hash(self):
        return read_page(self.session, f'https://{self.hostname}/member.php?mod=logging&action=login',
                         '读取登录页', lambda r: form_fields(r, True), verify=False)

    def login(self):
        """Login with username and password"""
        loginhash, formhash = self.login_form_hash()
        login_url = f'https://{self.hostname}/member.php?mod=logging&action=login&loginsubmit=yes' \
                    f'&handlekey=register&loginhash={loginhash}&inajax=1'


        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/'
        payload = {
            'formhash': formhash,
            'referer': f'https://{self.hostname}/',
            'username': self.username,
            'password': self.password,
            'questionid': self.questionid,
            'answer': self.answer
        }

        resp = send_once(self.session, login_url, '提交登录', proxies=self.proxies,
                         data=payload, headers=headers, verify=False)
        if any(word in text(resp) for word in ('登录失败', '密码错误', '密码错误次数')):
            raise TaskError('确认登录', '论坛拒绝登录，请核对账号、密码和安全问题；不重试登录')
        def authenticated(home):
            soup = BeautifulSoup(text(home), 'html.parser')
            if not any('action=logout' in a.get('href', '') for a in soup.find_all('a')):
                raise TaskError('确认登录', '未找到已登录标志，可能需要验证或登录被拒绝')
            return True
        read_page(self.session, f'https://{self.hostname}/home.php', '读取登录状态',
                  authenticated, verify=False)
        logger.info('Forum login confirmed by authenticated logout link.')

    def credit(self):
        credit_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu"
        def parse(response):
            body = text(response)
            inner = re.search(r'<!\[CDATA\[(.*?)\]\]>', body, re.S)
            soup = BeautifulSoup(inner.group(1) if inner else body, 'html.parser')
            element = soup.find('span', id='hcredit_2')
            value = element.get_text(strip=True) if element else ''
            if not re.fullmatch(r'[\d,]+', value):
                raise TaskError('查询银币', '页面缺少有效的银币余额字段')
            return value
        return read_page(self.session, credit_url, '查询银币', parse, verify=False)

    def space_form_hash(self):
        return read_page(self.session, f'https://{self.hostname}/home.php', '读取空间页', form_fields, verify=False)

    def space(self):
        formhash = self.space_form_hash()
        space_url = f"https://{self.hostname}/home.php?mod=spacecp&ac=doing&handlekey=doing&inajax=1"

        headers = copy(self._common_headers)
        headers["origin"] = f'https://{self.hostname}'
        headers["referer"] = f'https://{self.hostname}/home.php'

        for x in range(5):
            payload = {
                "message": "开心赚银币 {0} 次".format(x + 1).encode("GBK"),
                "addsubmit": "true",
                "spacenote": "true",
                "referer": "home.php",
                "formhash": formhash
            }
            resp = send_once(self.session, space_url, f'发布第 {x + 1} 条动态',
                             proxies=self.proxies, data=payload, headers=headers, verify=False)
            if '操作成功' in text(resp):
                self.completed += 1
                logger.info('Dynamic %s/5 published successfully.', self.completed)
                if x < 4:
                    time.sleep(120)
            else:
                raise TaskError(f'发布第 {x + 1} 条动态', '响应没有明确成功标志，结果待核实，不自动重试', True)


def main():
    check_only = '--check-login' in sys.argv
    client = None
    error = balance = None
    credit_warning = False
    try:
        if any(not os.environ.get(key) for key in ('SOUSHUBA_HOSTNAME', 'SOUSHUBA_USERNAME', 'SOUSHUBA_PASSWORD')):
            raise TaskError('配置检查', '缺少必需的 GitHub Secret')
        entry = os.environ['SOUSHUBA_HOSTNAME'].strip()
        if not entry.startswith(('http://', 'https://')):
            entry = 'http://' + entry
        redirect_url = get_refresh_url(entry)
        time.sleep(2)
        redirect_url2 = get_refresh_url(redirect_url)
        url = get_url(redirect_url2)
        logger.info('Forum address resolved.')
        client = SouShuBaClient(urlparse(url).hostname,
                                os.environ.get('SOUSHUBA_USERNAME', "USERNAME"),
                                os.environ.get('SOUSHUBA_PASSWORD', "PASSWORD"))
        client.login()
        if check_only:
            logger.info('Login check completed; no dynamics were submitted.')
        else:
            client.space()
            try:
                balance = client.credit()
                logger.info('Account balance: %s coins.', balance)
            except TaskError as exc:
                credit_warning = True
                logger.warning('%s；动态已全部发布，无需因此重跑。', exc)
    except TaskError as exc:
        error = exc
        logger.error('%s', exc)
    except Exception as exc:
        error = TaskError('未分类异常', type(exc).__name__, True)
        logger.error('%s', error)
    finally:
        write_summary(check_only, client.completed if client else 0, error, balance, credit_warning)
        if client:
            client.session.close()
    return 1 if error else 0


if __name__ == '__main__':
    sys.exit(main())
