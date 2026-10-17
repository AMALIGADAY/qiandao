"""Bounded GET retries and conservative single-attempt POSTs."""
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)
TIMEOUT = (10, 30)
READ_DELAYS = (10, 30)
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class TaskError(RuntimeError):
    def __init__(self, stage, reason, uncertain=False):
        self.stage, self.reason, self.uncertain = stage, reason, uncertain
        super().__init__(f'{stage}: {reason}')


def text(response):
    data = response.content
    declared = re.search(br'(?:charset\s*=\s*[\"\x27]?|encoding\s*=\s*[\"\x27])([\w-]+)', data[:4096], re.I)
    encoding = declared.group(1).decode('ascii') if declared else response.encoding
    if not encoding or encoding.lower() in ('iso-8859-1', 'ascii'):
        encoding = response.apparent_encoding or 'utf-8'
    try:
        return data.decode(encoding, errors='replace')
    except LookupError:
        return data.decode('utf-8', errors='replace')


def database_error(response):
    return 'discuz! database error' in text(response).lower()


def read_page(session, url, stage, parser=None, document_statuses=(), **kwargs):
    if not url or urlparse(url).scheme not in ('http', 'https') or not urlparse(url).hostname:
        raise TaskError(stage, '入口未返回有效的 HTTP(S) 地址')
    for attempt in range(3):
        try:
            response = session.get(url, timeout=TIMEOUT, **kwargs)
            if response.status_code in document_statuses and parser:
                # Upstream entry pages sometimes serve public meta-refresh links
                # with an error status. Only an explicitly expected, valid link
                # is accepted; no challenge solving or denial retry is performed.
                try:
                    return parser(response)
                except TaskError:
                    raise requests.HTTPError(response=response) from None
            if response.status_code in TRANSIENT_STATUS:
                raise TaskError(stage, f'服务器暂时不可用（HTTP {response.status_code}）')
            if response.status_code >= 400:
                raise requests.HTTPError(response=response)
            if database_error(response):
                raise TaskError(stage, '论坛返回数据库错误页面')
            return parser(response) if parser else response
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 'unknown'
            raise TaskError(stage, f'HTTP {status}，停止读取') from None
        except requests.exceptions.SSLError:
            raise TaskError(stage, 'TLS 证书验证失败') from None
        except requests.RequestException as exc:
            problem = TaskError(stage, f'读取网络异常（{type(exc).__name__}）')
        except TaskError as exc:
            problem = exc
        if attempt == 2:
            raise problem from None
        delay = READ_DELAYS[attempt]
        logger.warning('%s；%s 秒后重试读取（%s/3）', problem, delay, attempt + 2)
        time.sleep(delay)


def send_once(session, url, stage, **kwargs):
    try:
        # Never follow a credential-bearing POST to another host.
        response = session.post(url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
    except requests.RequestException as exc:
        raise TaskError(stage, f'提交网络异常（{type(exc).__name__}），结果不确定，不自动重试', True) from None
    if database_error(response):
        raise TaskError(stage, '论坛数据库异常，未确认提交成功，不自动重试', True)
    if 300 <= response.status_code < 400:
        raise TaskError(stage, '提交返回重定向，未跟随跳转；结果待核实', True)
    if response.status_code >= 400:
        raise TaskError(stage, f'提交返回 HTTP {response.status_code}，未确认成功，不自动重试', True)
    return response


def write_summary(check_only, completed, error=None, balance=None, credit_warning=False):
    status = '失败' if error else ('登录验证通过' if check_only else '发布完成')
    lines = ['## 搜书吧运行结果', '', f'- 状态：{status}',
             f'- 模式：{"仅验证登录，不发布" if check_only else "发布动态"}',
             f'- 已确认发布：{completed}/5 条']
    if balance is not None:
        lines.append(f'- 运行结束时银币余额：{balance}（不是本次新增数量）')
    if credit_warning:
        lines.append('- 全部动态已确认成功，但余额查询失败；无需因此重跑发布。')
    if error:
        lines.extend([f'- 失败阶段：{error.stage}', f'- 原因：{error.reason}'])
        if error.uncertain:
            lines.append('- 最近一次提交结果不确定。先在论坛核对记录，不要直接重跑整轮。')
        elif completed:
            lines.append('- 本轮已有成功发布，重跑整轮可能产生重复动态。')
    summary = '\n'.join(lines) + '\n'
    logger.info('Result: %s; confirmed dynamics: %s/5.', status, completed)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        try:
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(summary)
        except OSError:
            logger.warning('无法写入运行摘要，请查看日志中的结果。')
