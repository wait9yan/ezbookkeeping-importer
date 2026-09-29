CREATE TABLE schema_version(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_version(version) VALUES (1);
CREATE TABLE email_sync_checkpoint(
 source_id text NOT NULL, folder text NOT NULL, uid_validity text NOT NULL,
 registered_uid bigint NOT NULL CHECK(registered_uid>=0), initial_scan_upper_uid bigint NOT NULL CHECK(initial_scan_upper_uid>=0),
 last_scanned_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(source_id,folder));
CREATE TABLE email(
 id text PRIMARY KEY CHECK(id ~ '^[a-f0-9]{64}$'), raw_path text NOT NULL,
 subject text, sender_address text, sent_at timestamptz, header_message_id text,
 collected_at timestamptz NOT NULL DEFAULT now(), parse_status text NOT NULL DEFAULT 'pending' CHECK(parse_status IN ('pending','parsed','ignored','failed')),
 parsed_at timestamptz, parser_version text, report_key text,
 parse_issues jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(parse_issues)='array'), last_resolution jsonb,
 UNIQUE(id,report_key));
CREATE TABLE email_source_item(
 id bigserial PRIMARY KEY, source_id text NOT NULL, folder text NOT NULL, uid_validity text NOT NULL, uid bigint NOT NULL CHECK(uid>0),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','skipped','collected','failed')),
 email_id text REFERENCES email(id), skip_reason text, last_error text,
 discovered_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 source_status text CHECK(source_status IN ('verified','requires_acceptance')), source_reason text,
 collected_at timestamptz, accepted_at timestamptz, acceptance_reason text,
 UNIQUE(source_id,folder,uid_validity,uid),
 CHECK((status='collected' AND email_id IS NOT NULL AND source_status IS NOT NULL AND source_reason IS NOT NULL AND collected_at IS NOT NULL)
 OR (status<>'collected' AND email_id IS NULL AND source_status IS NULL AND source_reason IS NULL AND collected_at IS NULL)),
 CHECK((status='skipped')=(skip_reason IS NOT NULL)), CHECK((status='failed')=(last_error IS NOT NULL)),
 CHECK((accepted_at IS NULL AND acceptance_reason IS NULL) OR (status='collected' AND accepted_at IS NOT NULL AND acceptance_reason IS NOT NULL AND length(trim(acceptance_reason))>0)));
CREATE INDEX pending_source_items ON email_source_item(source_id,folder,uid_validity,uid) WHERE status IN ('pending','failed');
CREATE TABLE bank_report(
 report_key text PRIMARY KEY CHECK(report_key<>'' AND position(':' in report_key)=0), source_id text NOT NULL,
 bank_code text NOT NULL, report_type text NOT NULL CHECK(report_type IN ('daily','repayment','monthly')),
 report_date date, period_start date, period_end date, content_fingerprint text NOT NULL,
 source_email_id text NOT NULL REFERENCES email(id), parser_version text NOT NULL, content jsonb NOT NULL,
 accepted_at timestamptz NOT NULL DEFAULT now(), reconciliation_last_attempt_at timestamptz, reconciled_at timestamptz,
 reconciliation_next_check_at timestamptz, reconciliation_input_fingerprint text, reconciliation_queries_succeeded boolean,
 reconciliation_last_error text, reconciliation_version integer NOT NULL DEFAULT 0 CHECK(reconciliation_version>=0),
 FOREIGN KEY(source_email_id,report_key) REFERENCES email(id,report_key) DEFERRABLE INITIALLY DEFERRED,
 CHECK(report_type='monthly' OR (reconciliation_last_attempt_at IS NULL AND reconciled_at IS NULL AND reconciliation_next_check_at IS NULL
 AND reconciliation_input_fingerprint IS NULL AND reconciliation_queries_succeeded IS NULL AND reconciliation_last_error IS NULL AND reconciliation_version=0)));
ALTER TABLE email ADD FOREIGN KEY(report_key) REFERENCES bank_report(report_key);
CREATE TABLE bank_transactions(
 id text PRIMARY KEY CHECK(id ~ '^[A-Za-z0-9_-]{16}$'), report_key text NOT NULL REFERENCES bank_report(report_key),
 report_row_key text NOT NULL CHECK(report_row_key<>'' AND position(':' in report_row_key)=0),
 event_type text NOT NULL CHECK(event_type IN ('expense','refund','repayment')), occurred_date date NOT NULL, occurred_at timestamptz,
 time_precision text NOT NULL, merchant_name text NOT NULL, card_reference text, original_amount numeric NOT NULL,
 original_currency text, posted_date date, bank_settlement_amount numeric, bank_settlement_currency text,
 source_details jsonb NOT NULL DEFAULT '{}', source_marker text GENERATED ALWAYS AS ('ebki-' || id) STORED UNIQUE,
 ledger_transaction_id text UNIQUE, decision_version integer NOT NULL DEFAULT 1 CHECK(decision_version>0),
 import_status text NOT NULL DEFAULT 'pending' CHECK(import_status IN ('pending','queued','dispatching','unknown','booked','issue','ignored')),
 import_decision jsonb, settlement_adjustment jsonb, import_error jsonb, last_resolution jsonb,
 UNIQUE(report_key,report_row_key));
CREATE TABLE background_task(
 id bigserial PRIMARY KEY, task_type text NOT NULL CHECK(task_type IN ('sync','sync_range','create','settle_amount','settle_currency')),
 bank_transaction_id text REFERENCES bank_transactions(id), decision_version integer,
 operation_key text NOT NULL UNIQUE, payload jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','dispatching','unknown','done','rejected','cancelled')),
 ledger_transaction_id text, completion_method text CHECK(completion_method IN ('write_verified','existing_link','already_applied')),
 last_error text, error_code text, last_resolution jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK((task_type IN ('sync','sync_range') AND bank_transaction_id IS NULL AND decision_version IS NULL AND status NOT IN ('unknown','rejected'))
 OR (task_type IN ('create','settle_amount','settle_currency') AND bank_transaction_id IS NOT NULL AND decision_version IS NOT NULL AND decision_version>0)));
CREATE UNIQUE INDEX active_sync ON background_task(task_type) WHERE task_type='sync' AND status IN ('queued','dispatching');
CREATE UNIQUE INDEX active_write ON background_task(bank_transaction_id) WHERE task_type IN ('create','settle_amount','settle_currency') AND status IN ('queued','dispatching','unknown');
CREATE TABLE ledger_write_attempt(
 id bigserial PRIMARY KEY, task_id bigint NOT NULL REFERENCES background_task(id), decision_version integer NOT NULL CHECK(decision_version>0),
 request jsonb NOT NULL, outcome text NOT NULL CHECK(outcome IN ('unknown','rejected','confirmed')), response jsonb, error text,
 created_at timestamptz NOT NULL DEFAULT now(), response_received_at timestamptz, verified_at timestamptz);
CREATE TABLE bank_statement_reconciliation(
 id bigserial PRIMARY KEY, statement_report_key text NOT NULL REFERENCES bank_report(report_key),
 check_direction text NOT NULL CHECK(check_direction IN ('statement_to_transaction','transaction_to_statement')),
 statement_row_key text, bank_transaction_id text REFERENCES bank_transactions(id),
 match_status text NOT NULL CHECK(match_status IN ('matched','missing_source_transaction','missing_statement_evidence','ambiguous','out_of_scope','awaiting_statement')),
 ledger_check_status text NOT NULL CHECK(ledger_check_status IN ('not_checked','matched','mismatched','target_missing','query_failed','not_comparable')),
 expected_amount numeric, expected_currency text, actual_amount numeric, actual_currency text,
 checked_at timestamptz NOT NULL DEFAULT now(), ledger_observed_at timestamptz, details jsonb NOT NULL DEFAULT '{}', last_error text,
 CHECK((check_direction='statement_to_transaction' AND statement_row_key IS NOT NULL AND match_status IN ('matched','missing_source_transaction','ambiguous','out_of_scope'))
 OR (check_direction='transaction_to_statement' AND bank_transaction_id IS NOT NULL AND match_status IN ('matched','missing_statement_evidence','ambiguous','awaiting_statement'))),
 CHECK(match_status<>'matched' OR (statement_row_key IS NOT NULL AND bank_transaction_id IS NOT NULL)),
 CHECK(check_direction<>'statement_to_transaction' OR match_status NOT IN ('ambiguous','missing_source_transaction') OR bank_transaction_id IS NULL),
 CHECK(ledger_check_status='not_checked' OR bank_transaction_id IS NOT NULL),
 CHECK(ledger_check_status<>'query_failed' OR last_error IS NOT NULL),
 CHECK(ledger_check_status NOT IN ('query_failed','not_checked','target_missing') OR (actual_amount IS NULL AND actual_currency IS NULL AND ledger_observed_at IS NULL)));
CREATE UNIQUE INDEX statement_object ON bank_statement_reconciliation(statement_report_key,statement_row_key) WHERE check_direction='statement_to_transaction';
CREATE UNIQUE INDEX transaction_object ON bank_statement_reconciliation(statement_report_key,bank_transaction_id) WHERE check_direction='transaction_to_statement';
COMMENT ON TABLE schema_version IS '数据库结构版本';
COMMENT ON COLUMN schema_version.version IS '数据库结构版本号';
COMMENT ON COLUMN schema_version.applied_at IS '结构版本应用时间';
COMMENT ON TABLE email_sync_checkpoint IS '邮件同步检查点';
COMMENT ON COLUMN email_sync_checkpoint.source_id IS '业务邮箱来源标识';
COMMENT ON COLUMN email_sync_checkpoint.folder IS '邮箱文件夹';
COMMENT ON COLUMN email_sync_checkpoint.uid_validity IS '邮件编号有效性标识';
COMMENT ON COLUMN email_sync_checkpoint.registered_uid IS '已登记邮件编号上界';
COMMENT ON COLUMN email_sync_checkpoint.initial_scan_upper_uid IS '首次历史扫描上界';
COMMENT ON COLUMN email_sync_checkpoint.last_scanned_at IS '最近扫描登记时间';
COMMENT ON TABLE email IS '邮件原件';
COMMENT ON COLUMN email.id IS '邮件原件标识';
COMMENT ON COLUMN email.raw_path IS '邮件原件路径';
COMMENT ON COLUMN email.subject IS '邮件主题';
COMMENT ON COLUMN email.sender_address IS '邮件头发件地址';
COMMENT ON COLUMN email.sent_at IS '邮件头发送时间';
COMMENT ON COLUMN email.header_message_id IS '邮件头消息标识';
COMMENT ON COLUMN email.collected_at IS '原件首次保存时间';
COMMENT ON COLUMN email.parse_status IS '邮件解析状态';
COMMENT ON COLUMN email.parsed_at IS '最近解析结束时间';
COMMENT ON COLUMN email.parser_version IS '邮件解析器版本';
COMMENT ON COLUMN email.report_key IS '关联银行报告标识';
COMMENT ON COLUMN email.parse_issues IS '当前解析问题';
COMMENT ON COLUMN email.last_resolution IS '最近人工处理记录';
COMMENT ON TABLE email_source_item IS '邮件来源项';
COMMENT ON COLUMN email_source_item.id IS '邮件来源项编号';
COMMENT ON COLUMN email_source_item.source_id IS '业务邮箱来源标识';
COMMENT ON COLUMN email_source_item.folder IS '邮箱文件夹';
COMMENT ON COLUMN email_source_item.uid_validity IS '邮件编号有效性标识';
COMMENT ON COLUMN email_source_item.uid IS '邮箱邮件编号';
COMMENT ON COLUMN email_source_item.status IS '邮件采集状态';
COMMENT ON COLUMN email_source_item.email_id IS '关联邮件原件标识';
COMMENT ON COLUMN email_source_item.skip_reason IS '邮件筛除原因';
COMMENT ON COLUMN email_source_item.last_error IS '最近采集错误';
COMMENT ON COLUMN email_source_item.discovered_at IS '来源首次登记时间';
COMMENT ON COLUMN email_source_item.updated_at IS '来源状态更新时间';
COMMENT ON COLUMN email_source_item.source_status IS '来源认证状态';
COMMENT ON COLUMN email_source_item.source_reason IS '来源认证原因';
COMMENT ON COLUMN email_source_item.collected_at IS '来源首次采集时间';
COMMENT ON COLUMN email_source_item.accepted_at IS '来源人工接纳时间';
COMMENT ON COLUMN email_source_item.acceptance_reason IS '来源人工接纳理由';
COMMENT ON TABLE bank_report IS '银行报告';
COMMENT ON COLUMN bank_report.report_key IS '银行报告标识';
COMMENT ON COLUMN bank_report.source_id IS '业务邮箱来源标识';
COMMENT ON COLUMN bank_report.bank_code IS '银行代码';
COMMENT ON COLUMN bank_report.report_type IS '银行报告类型';
COMMENT ON COLUMN bank_report.report_date IS '银行报告日期';
COMMENT ON COLUMN bank_report.period_start IS '账期开始日期';
COMMENT ON COLUMN bank_report.period_end IS '账期结束日期';
COMMENT ON COLUMN bank_report.content_fingerprint IS '报告业务内容指纹';
COMMENT ON COLUMN bank_report.source_email_id IS '报告依据原件标识';
COMMENT ON COLUMN bank_report.parser_version IS '报告解析器版本';
COMMENT ON COLUMN bank_report.content IS '已接纳报告内容';
COMMENT ON COLUMN bank_report.accepted_at IS '报告首次接纳时间';
COMMENT ON COLUMN bank_report.reconciliation_last_attempt_at IS '最近核对开始时间';
COMMENT ON COLUMN bank_report.reconciled_at IS '最近核对结果发布时间';
COMMENT ON COLUMN bank_report.reconciliation_next_check_at IS '下次计划核对时间';
COMMENT ON COLUMN bank_report.reconciliation_input_fingerprint IS '最近核对输入指纹';
COMMENT ON COLUMN bank_report.reconciliation_queries_succeeded IS '本轮账本查询成功标志';
COMMENT ON COLUMN bank_report.reconciliation_last_error IS '最近整轮核对错误';
COMMENT ON COLUMN bank_report.reconciliation_version IS '核对结果发布版本';
COMMENT ON TABLE bank_transactions IS '银行来源交易';
COMMENT ON COLUMN bank_transactions.id IS '银行来源交易标识';
COMMENT ON COLUMN bank_transactions.report_key IS '所属银行报告标识';
COMMENT ON COLUMN bank_transactions.report_row_key IS '来源报告行标识';
COMMENT ON COLUMN bank_transactions.event_type IS '银行交易事件类型';
COMMENT ON COLUMN bank_transactions.occurred_date IS '银行交易日期';
COMMENT ON COLUMN bank_transactions.occurred_at IS '银行交易发生时间';
COMMENT ON COLUMN bank_transactions.time_precision IS '银行交易时间精度';
COMMENT ON COLUMN bank_transactions.merchant_name IS '银行原文商户描述';
COMMENT ON COLUMN bank_transactions.card_reference IS '银行卡号或尾号';
COMMENT ON COLUMN bank_transactions.original_amount IS '银行原币金额';
COMMENT ON COLUMN bank_transactions.original_currency IS '银行原币币种';
COMMENT ON COLUMN bank_transactions.posted_date IS '银行入账日期';
COMMENT ON COLUMN bank_transactions.bank_settlement_amount IS '银行结算金额';
COMMENT ON COLUMN bank_transactions.bank_settlement_currency IS '银行结算币种';
COMMENT ON COLUMN bank_transactions.source_details IS '银行来源补充信息';
COMMENT ON COLUMN bank_transactions.source_marker IS '账本交易来源标记';
COMMENT ON COLUMN bank_transactions.ledger_transaction_id IS '已关联远端交易编号';
COMMENT ON COLUMN bank_transactions.decision_version IS '当前导入决定版本';
COMMENT ON COLUMN bank_transactions.import_status IS '交易导入状态';
COMMENT ON COLUMN bank_transactions.import_decision IS '当前导入决定';
COMMENT ON COLUMN bank_transactions.settlement_adjustment IS '已确认结算调整';
COMMENT ON COLUMN bank_transactions.import_error IS '当前导入决定错误';
COMMENT ON COLUMN bank_transactions.last_resolution IS '最近人工处理记录';
COMMENT ON TABLE background_task IS '后台任务';
COMMENT ON COLUMN background_task.id IS '后台任务编号';
COMMENT ON COLUMN background_task.task_type IS '后台任务类型';
COMMENT ON COLUMN background_task.bank_transaction_id IS '所属银行来源交易标识';
COMMENT ON COLUMN background_task.decision_version IS '任务授权决定版本';
COMMENT ON COLUMN background_task.operation_key IS '任务操作标识';
COMMENT ON COLUMN background_task.payload IS '任务执行内容';
COMMENT ON COLUMN background_task.status IS '任务执行状态';
COMMENT ON COLUMN background_task.ledger_transaction_id IS '任务远端交易编号';
COMMENT ON COLUMN background_task.completion_method IS '任务完成方式';
COMMENT ON COLUMN background_task.last_error IS '当前任务执行错误';
COMMENT ON COLUMN background_task.error_code IS '任务错误代码';
COMMENT ON COLUMN background_task.last_resolution IS '最近人工处理记录';
COMMENT ON COLUMN background_task.created_at IS '任务创建时间';
COMMENT ON COLUMN background_task.updated_at IS '任务更新时间';
COMMENT ON TABLE ledger_write_attempt IS '账本写入尝试';
COMMENT ON COLUMN ledger_write_attempt.id IS '账本写入尝试编号';
COMMENT ON COLUMN ledger_write_attempt.task_id IS '所属后台任务编号';
COMMENT ON COLUMN ledger_write_attempt.decision_version IS '写入尝试决定版本';
COMMENT ON COLUMN ledger_write_attempt.request IS '账本写入请求';
COMMENT ON COLUMN ledger_write_attempt.outcome IS '写入尝试结果';
COMMENT ON COLUMN ledger_write_attempt.response IS '账本写入响应';
COMMENT ON COLUMN ledger_write_attempt.error IS '写入尝试错误';
COMMENT ON COLUMN ledger_write_attempt.created_at IS '写入尝试登记时间';
COMMENT ON COLUMN ledger_write_attempt.response_received_at IS '账本响应接收时间';
COMMENT ON COLUMN ledger_write_attempt.verified_at IS '账本回读确认时间';
COMMENT ON TABLE bank_statement_reconciliation IS '银行月账单核对结果';
COMMENT ON COLUMN bank_statement_reconciliation.id IS '核对结果编号';
COMMENT ON COLUMN bank_statement_reconciliation.statement_report_key IS '所属月账单标识';
COMMENT ON COLUMN bank_statement_reconciliation.check_direction IS '账单核对方向';
COMMENT ON COLUMN bank_statement_reconciliation.statement_row_key IS '月账单行标识';
COMMENT ON COLUMN bank_statement_reconciliation.bank_transaction_id IS '关联银行来源交易标识';
COMMENT ON COLUMN bank_statement_reconciliation.match_status IS '银行证据匹配状态';
COMMENT ON COLUMN bank_statement_reconciliation.ledger_check_status IS '远端账本核对状态';
COMMENT ON COLUMN bank_statement_reconciliation.expected_amount IS '期望记账金额';
COMMENT ON COLUMN bank_statement_reconciliation.expected_currency IS '期望记账币种';
COMMENT ON COLUMN bank_statement_reconciliation.actual_amount IS '实际账本金额';
COMMENT ON COLUMN bank_statement_reconciliation.actual_currency IS '实际账本币种';
COMMENT ON COLUMN bank_statement_reconciliation.checked_at IS '本次核对时间';
COMMENT ON COLUMN bank_statement_reconciliation.ledger_observed_at IS '本次账本读取时间';
COMMENT ON COLUMN bank_statement_reconciliation.details IS '当前核对诊断详情';
COMMENT ON COLUMN bank_statement_reconciliation.last_error IS '本次账本查询错误';
