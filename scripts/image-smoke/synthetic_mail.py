"""确定性的单封合成邮件传输；解析、来源认证与落库仍由真实应用完成。"""
from email.message import EmailMessage


def daily_mail():
    message = EmailMessage()
    message['Subject'] = '每日信用管家'
    message['From'] = 'ccsvc@message.cmbchina.com'
    message['Message-ID'] = '<startup-smoke@example.test>'
    message['Received'] = 'from mail.cmbchina.com by mx.qq.com with ESMTP; Wed, 30 Sep 2026 12:00:00 +0800'
    message['Authentication-Results'] = (
        'mx.qq.com; spf=pass smtp.mailfrom=message.cmbchina.com; '
        'dkim=pass header.d=message.cmbchina.com; dmarc=pass header.from=message.cmbchina.com'
    )
    message.set_content(
        '<p>2026/09/30 您的消费明细如下：</p>'
        '<div><b>12:00:00</b><b>CNY 10.00</b><b>尾号1234 消费 合成商户</b></div>',
        subtype='html',
    )
    return message.as_bytes()


class SingleMailbox:
    def __init__(self, settings):
        self.raw = daily_mail()

    def folders(self):
        return ['INBOX']

    def scan(self, folder, after_uid=0, since=None, until=None):
        assert folder == 'INBOX'
        return '1', [1] if not after_uid or since is not None else []

    def fetch(self, folder, uid):
        assert folder == 'INBOX' and uid == 1
        return self.raw

    def fetch_headers(self, folder, uid):
        return self.fetch(folder, uid).split(b'\n\n', 1)[0] + b'\n\n'

    def fetch_headers_batch(self, folder, uids):
        return {uid: self.fetch_headers(folder, uid) for uid in uids}

    def close(self):
        pass
