-- 此定位段由一次性清理工具原样复用；临时表不属于运行时业务结构。
CREATE TEMP TABLE purge_repayment_emails ON COMMIT DROP AS
 SELECT id FROM email WHERE regexp_replace(regexp_replace(coalesce(subject,''), '^[[:space:]]+|[[:space:]]+$', '', 'g'),
 '^(?:(?:fw|fwd|转发)[[:space:]]*[:：][[:space:]]*)+', '', 'i')='自动还款扣款通知';
CREATE TEMP TABLE purge_repayment_reports ON COMMIT DROP AS
 SELECT report_key FROM bank_report WHERE report_type='repayment'
 OR source_email_id IN (SELECT id FROM purge_repayment_emails)
 OR report_key IN (SELECT report_key FROM email WHERE id IN (SELECT id FROM purge_repayment_emails))
 OR report_key IN (SELECT report_key FROM bank_transactions WHERE event_type='repayment');
INSERT INTO purge_repayment_emails
 SELECT id FROM email WHERE (report_key IN (SELECT report_key FROM purge_repayment_reports)
 OR id IN (SELECT source_email_id FROM bank_report WHERE report_key IN (SELECT report_key FROM purge_repayment_reports)))
 AND id NOT IN (SELECT id FROM purge_repayment_emails);
CREATE TEMP TABLE purge_repayment_transactions ON COMMIT DROP AS
 SELECT id FROM bank_transactions WHERE report_key IN (SELECT report_key FROM purge_repayment_reports);
CREATE TEMP TABLE purge_repayment_statements ON COMMIT DROP AS
 SELECT DISTINCT statement_report_key AS report_key FROM bank_statement_reconciliation
 WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions)
 UNION SELECT report_key FROM bank_report WHERE report_type='monthly'
 AND EXISTS (SELECT 1 FROM jsonb_array_elements(content->'rows') row WHERE row->>'event_type'='statement');
-- END REPAYMENT PURGE SELECTION
DO $purge$ DECLARE required boolean; BEGIN
 SELECT EXISTS (SELECT 1 FROM purge_repayment_emails)
 OR EXISTS (SELECT 1 FROM purge_repayment_reports)
 OR EXISTS (SELECT 1 FROM purge_repayment_statements) INTO required;
 IF required AND current_setting('ebki.repayment_purge_prepared',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION USING ERRCODE='P0004', MESSAGE='repayment cleanup requires scripts/purge-repayment-history.py';
 END IF;
END
$purge$;
DELETE FROM ledger_write_attempt WHERE task_id IN
 (SELECT id FROM background_task WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions));
DELETE FROM background_task WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions);
DELETE FROM bank_statement_reconciliation WHERE bank_transaction_id IN (SELECT id FROM purge_repayment_transactions)
 OR statement_report_key IN (SELECT report_key FROM purge_repayment_reports)
 OR statement_report_key IN (SELECT report_key FROM purge_repayment_statements);
UPDATE bank_report SET reconciliation_last_attempt_at=NULL,reconciled_at=NULL,
 reconciliation_next_check_at=NULL,reconciliation_input_fingerprint=NULL,
 reconciliation_queries_succeeded=NULL,reconciliation_last_error=NULL,
 reconciliation_version=reconciliation_version+1
 WHERE report_key IN (SELECT report_key FROM purge_repayment_statements) AND report_type='monthly';
DELETE FROM bank_transactions WHERE id IN (SELECT id FROM purge_repayment_transactions);
UPDATE email SET report_key=NULL WHERE report_key IN (SELECT report_key FROM purge_repayment_reports);
DELETE FROM bank_report WHERE report_key IN (SELECT report_key FROM purge_repayment_reports);
DELETE FROM email_source_item WHERE email_id IN (SELECT id FROM purge_repayment_emails);
DELETE FROM email WHERE id IN (SELECT id FROM purge_repayment_emails);
ALTER TABLE bank_report DROP CONSTRAINT bank_report_report_type_check;
ALTER TABLE bank_report ADD CONSTRAINT bank_report_report_type_check CHECK(report_type IN ('daily','monthly'));
ALTER TABLE bank_transactions DROP CONSTRAINT bank_transactions_event_type_check;
ALTER TABLE bank_transactions ADD CONSTRAINT bank_transactions_event_type_check CHECK(event_type IN ('expense','refund'));
