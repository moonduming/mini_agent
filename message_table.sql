CREATE TABLE conversations (
    id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,

    -- 当前累计的上下文摘要
    summary TEXT,

    -- summary 已经覆盖到哪一个完整轮次
    -- 例如为 12，表示 turn_id <= 12 的消息已经进入 summary
    summary_until_turn_id BIGINT,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);


CREATE TABLE conversation_messages (
    id BIGSERIAL PRIMARY KEY,

    conversation_id UUID NOT NULL
        REFERENCES conversations(id)
        ON DELETE CASCADE,

    turn_id BIGINT NOT NULL,

    -- human / ai / tool / system
    message_type VARCHAR(32) NOT NULL,

    -- pending / success / timeout / failed / cancelled
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'success',
            'timeout',
            'failed',
            'cancelled'
        )),

    -- 失败、超时、取消时记录错误信息
    error_message TEXT,

    -- 完整保存 LangChain Message
    -- 包括 content、tool_calls、tool_call_id 等
    message_data JSONB NOT NULL,

    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);


CREATE INDEX idx_conversation_messages_conversation_id_id
    ON conversation_messages(conversation_id, id);
