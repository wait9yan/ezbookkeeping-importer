CREATE TABLE transaction_source(
 report_key text NOT NULL REFERENCES bank_report(report_key),
 row_key text NOT NULL CHECK(row_key<>'' AND position(':' in row_key)=0),
 transaction_id text REFERENCES bank_transactions(id),
 resolution_version integer NOT NULL DEFAULT 0 CHECK(resolution_version>=0),
 last_resolution jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(report_key,row_key),
 UNIQUE(report_key,transaction_id),
 UNIQUE(report_key,row_key,transaction_id));
INSERT INTO transaction_source(report_key,row_key,transaction_id)
 SELECT report_key,report_row_key,id FROM bank_transactions;
ALTER TABLE bank_transactions ADD CONSTRAINT initial_transaction_source
 FOREIGN KEY(report_key,report_row_key,id)
 REFERENCES transaction_source(report_key,row_key,transaction_id) DEFERRABLE INITIALLY DEFERRED;
INSERT INTO transaction_source(report_key,row_key)
 SELECT r.report_key,j->>'row_key' FROM bank_report r,
 jsonb_array_elements(r.content->'rows') j
 WHERE j->>'event_type' IN ('expense','refund')
 ON CONFLICT(report_key,row_key) DO NOTHING;
ALTER TABLE bank_statement_reconciliation DROP CONSTRAINT bank_statement_reconciliation_ledger_check_status_check;
ALTER TABLE bank_statement_reconciliation ADD CHECK(ledger_check_status IN
 ('not_checked','matched','mismatched','target_missing','query_failed','not_comparable','candidate_found','no_candidate','ambiguous'));
ALTER TABLE bank_statement_reconciliation DROP CONSTRAINT bank_statement_reconciliation_check3;
ALTER TABLE bank_statement_reconciliation ADD CHECK(ledger_check_status NOT IN ('matched','mismatched','target_missing') OR bank_transaction_id IS NOT NULL);
COMMENT ON TABLE transaction_source IS '交易来源关联';
COMMENT ON COLUMN transaction_source.report_key IS '来源报告标识';
COMMENT ON COLUMN transaction_source.row_key IS '来源报告行标识';
COMMENT ON COLUMN transaction_source.transaction_id IS '内部交易标识';
COMMENT ON COLUMN transaction_source.resolution_version IS '来源决定版本';
COMMENT ON COLUMN transaction_source.last_resolution IS '最近人工处理记录';
COMMENT ON COLUMN transaction_source.created_at IS '来源创建时间';
COMMENT ON COLUMN transaction_source.updated_at IS '来源更新时间';
COMMENT ON COLUMN bank_transactions.report_key IS '初始身份来源报告标识';
COMMENT ON COLUMN bank_transactions.report_row_key IS '初始身份来源行标识';
