
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import logging
from database import db, parse_bool_value, parse_int_value
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def exception_type(exc: Exception) -> str:
    return exc.__class__.__name__

class Notifier:
    """
    负责电子邮件通知发送，包括下班关怀提醒和系统异常告警。
    """
    
    def __init__(self):
        self.reload_config()

    def reload_config(self):
        """从数据库读取最新的邮件服务器配置"""
        self.smtp_host = db.get_config('email_host') or db.get_config('smtp_host')
        self.smtp_port = parse_int_value(
            db.get_config('email_port') or db.get_config('smtp_port'),
            465,
            minimum=1,
            maximum=65535,
        )
        self.smtp_user = db.get_config('email_user') or db.get_config('smtp_user')
        self.smtp_pass = db.get_config('email_pass') or db.get_config('smtp_pass')
        self.receiver = db.get_config('email_to') or db.get_config('notify_receiver')
        email_enabled = db.get_config('email_enabled')
        self.enabled = parse_bool_value(
            email_enabled if email_enabled is not None else db.get_config('notify_enabled'),
            False,
        )

    def send_email(self, subject, content, is_html=False):
        """通用发信逻辑"""
        if not self.enabled or not self.smtp_host or not self.smtp_user:
            logger.warning("Email Notify: 未启用或未配置 SMTP。")
            return False

        try:
            msg = MIMEText(content, 'html' if is_html else 'plain', 'utf-8')
            msg['From'] = self.smtp_user
            msg['To'] = self.receiver
            msg['Subject'] = Header(subject, 'utf-8')

            # 支持 SSL/TLS
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                server.starttls()
            
            server.login(self.smtp_user, self.smtp_pass)
            server.sendmail(self.smtp_user, [self.receiver], msg.as_string())
            server.quit()
            logger.info("Email Notify: 成功发送邮件")
            return True
        except Exception as e:
            logger.error("Email Notify Error: %s", exception_type(e))
            return False

    def send_off_work_reminder(self, movies_downloaded):
        """下班温情汇总提醒"""
        if not movies_downloaded:
            return
            
        subject = "您的订阅内容已准备就绪"
        body = f"""
        <html>
        <body>
            <h3>辛苦工作一天了，下班回家休息一下吧！</h3>
            <p>为您送上今日的高级关怀：</p>
            <ul>
                {"".join([f"<li>{m['title']}</li>" for m in movies_downloaded])}
            </ul>
            <p>内容已自动整理到媒体库。祝您有个愉快的夜晚。</p>
        </body>
        </html>
        """
        self.send_email(subject, body, is_html=True)

    def send_system_alert(self, alert_type, message):
        """系统告警提醒 (磁盘/连接等)。返回是否真的发出去了。"""
        subject = f"【系统告警】BusTerminal - {alert_type}"
        body = f"警告内容: {message}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        return self.send_email(subject, body)

    def send_watching_digest(self, stats):
        """观看提醒邮件：长时间未观看时，把片单状况做成带图表的汇总发过去。

        should_notify 为False（还没到提醒阈值）时直接跳过，不浪费一次发信额度。
        """
        if not stats or not stats.get("should_notify"):
            return False
        from insight_email_template import build_watching_digest_email
        subject, body = build_watching_digest_email(stats)
        logger.info(
            "Email Notify: 发送观看提醒 idle_days=%s unwatched=%s",
            stats.get("idle_days_actual"), stats.get("unwatched_total"),
        )
        return self.send_email(subject, body, is_html=True)

notifier = Notifier()
