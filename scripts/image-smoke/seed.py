"""仅向随机隔离验收数据库写入合成待写交易。"""
import os
import sys
from datetime import date

from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore


def seed(key):
    identifier = {'before': 'a' * 16, 'after': 'b' * 16}[key]
    email = {'before': 'a' * 64, 'after': 'b' * 64}[key]
    report = 'smoke-' + key
    payload = {
        'type': 3, 'categoryId': '11', 'time': 1790726400, 'utcOffset': 480,
        'sourceAccountId': '1', 'sourceAmount': 10000, 'hideAmount': False,
        'tagIds': [], 'comment': 'synthetic ebki-' + identifier,
    }
    store = PostgresStore(os.environ['EBKI_DATABASE_URL'])
    try:
        with store.transaction():
            store.execute("INSERT INTO email(id,raw_path,parse_status) VALUES (%s,%s,'parsed')",
                          (email, '/unused-synthetic'))
            store.execute('''INSERT INTO bank_report(report_key,source_id,bank_code,report_type,
                report_date,content_fingerprint,source_email_id,parser_version,content)
                VALUES (%s,'isolated-smoke','cmb','daily',%s,%s,%s,'synthetic',%s)''',
                          (report, date(2026, 9, 30), key, email, {}))
            store.execute('UPDATE email SET report_key=%s WHERE id=%s', (report, email))
            store.execute('''INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,
                occurred_date,time_precision,merchant_name,original_amount,original_currency,
                import_status,import_decision)
                VALUES (%s,%s,'row','expense',%s,'second','synthetic',100,'CNY','queued',%s)''',
                          (identifier, report, date(2026, 9, 30),
                           {'payload': payload, 'target_currency': 'CNY'}))
            store.execute('''INSERT INTO background_task(task_type,bank_transaction_id,
                decision_version,operation_key,payload) VALUES ('create',%s,1,%s,%s)''',
                          (identifier, 'smoke-' + key, payload))
    finally:
        store.close()


if __name__ == '__main__':
    seed(sys.argv[1])
