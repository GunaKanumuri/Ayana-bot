-- ═══════════════════════════════════════════════════════════
-- AYANA Care Companion — Supabase Schema
-- Run this in Supabase SQL Editor (all at once)
-- ═══════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ═══════════════ FAMILIES ═══════════════
CREATE TABLE IF NOT EXISTS families (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    plan TEXT DEFAULT 'trial' CHECK (plan IN ('trial', 'active', 'expired', 'free')),
    trial_ends_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '14 days'),
    report_format TEXT DEFAULT 'combined' CHECK (report_format IN ('combined', 'separate')),
    razorpay_subscription_id TEXT,
    stripe_customer_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════ CHILDREN ═══════════════
CREATE TABLE IF NOT EXISTS children (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id UUID REFERENCES families(id) ON DELETE CASCADE,
    phone TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    report_time TIME DEFAULT '20:00',
    timezone TEXT DEFAULT 'Asia/Kolkata',
    is_primary BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════ PARENTS ═══════════════
CREATE TABLE IF NOT EXISTS parents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id UUID REFERENCES families(id) ON DELETE CASCADE,
    phone TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    nickname TEXT NOT NULL,
    language TEXT DEFAULT 'te',
    tts_voice TEXT DEFAULT 'roopa',
    checkin_time TIME DEFAULT '08:00',
    routine JSONB DEFAULT '{}',
    activities JSONB DEFAULT '[]',
    conditions JSONB DEFAULT '[]',
    alone_during_day BOOLEAN DEFAULT FALSE,
    training_day INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    paused_until DATE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════ MEDICINE GROUPS ═══════════════
CREATE TABLE IF NOT EXISTS medicine_groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    anchor_event TEXT NOT NULL CHECK (anchor_event IN (
        'wake', 'before_food', 'after_food', 'afternoon', 'evening', 'dinner', 'after_dinner', 'night'
    )),
    time_window TIME NOT NULL,
    sort_order INTEGER DEFAULT 0
);

-- ═══════════════ MEDICINES ═══════════════
CREATE TABLE IF NOT EXISTS medicines (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    group_id UUID REFERENCES medicine_groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    instructions TEXT,
    is_as_needed BOOLEAN DEFAULT FALSE,
    trigger_symptom TEXT
);

-- ═══════════════ MESSAGE VARIATIONS ═══════════════
CREATE TABLE IF NOT EXISTS message_variations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    touchpoint TEXT NOT NULL CHECK (touchpoint IN (
        'morning_greeting', 'food_check', 'medicine_before_food',
        'medicine_after_food', 'medicine_night', 'activity_check',
        'evening_checkin', 'anything_else', 'goodnight'
    )),
    message_text TEXT NOT NULL,
    is_ai_generated BOOLEAN DEFAULT TRUE,
    is_selected BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════ CHECK-INS ═══════════════
CREATE TABLE IF NOT EXISTS check_ins (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    touchpoint TEXT NOT NULL,
    status TEXT DEFAULT 'sent' CHECK (status IN ('sent', 'replied', 'missed', 'skipped')),
    mood TEXT,
    raw_reply TEXT,
    raw_audio_url TEXT,
    concerns JSONB DEFAULT '[]',
    medicine_taken JSONB DEFAULT '{}',
    ai_extraction JSONB DEFAULT '{}',
    sent_at TIMESTAMPTZ,
    replied_at TIMESTAMPTZ,
    variation_id UUID REFERENCES message_variations(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(parent_id, date, touchpoint)
);

-- ═══════════════ HEALTH FLOWS ═══════════════
CREATE TABLE IF NOT EXISTS health_flows (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    condition TEXT NOT NULL,
    state TEXT DEFAULT 'active' CHECK (state IN ('active', 'recovery', 'confirmation', 'resolved')),
    details JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- ═══════════════ ALERTS ═══════════════
CREATE TABLE IF NOT EXISTS alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id UUID REFERENCES families(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES parents(id),
    type TEXT NOT NULL CHECK (type IN (
        'emergency', 'missed_checkin', 'concern_pattern',
        'severe_pain', 'missed_streak', 'no_food'
    )),
    message TEXT NOT NULL,
    context JSONB DEFAULT '{}',
    sent_to JSONB DEFAULT '[]',
    call_attempted BOOLEAN DEFAULT FALSE,
    call_answered BOOLEAN DEFAULT FALSE,
    acknowledged BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════ CONCERN LOG ═══════════════
CREATE TABLE IF NOT EXISTS concern_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    concern_text TEXT NOT NULL,
    category TEXT,
    severity TEXT CHECK (severity IN ('mild', 'moderate', 'severe')),
    first_seen DATE DEFAULT CURRENT_DATE,
    last_seen DATE DEFAULT CURRENT_DATE,
    frequency INTEGER DEFAULT 1,
    is_resolved BOOLEAN DEFAULT FALSE
);

-- ═══════════════ SPECIAL DATES ═══════════════
CREATE TABLE IF NOT EXISTS special_dates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    date_type TEXT NOT NULL CHECK (date_type IN ('birthday', 'anniversary', 'festival', 'custom')),
    label TEXT NOT NULL,
    date_value DATE NOT NULL,
    recurring BOOLEAN DEFAULT TRUE
);

-- ═══════════════ CONVERSATION STATE ═══════════════
-- Tracks where each parent is in today's conversation flow
CREATE TABLE IF NOT EXISTS conversation_state (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id UUID REFERENCES parents(id) ON DELETE CASCADE,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    current_touchpoint TEXT,
    awaiting_response BOOLEAN DEFAULT FALSE,
    touchpoints_completed JSONB DEFAULT '[]',
    touchpoints_remaining JSONB DEFAULT '[]',
    context JSONB DEFAULT '{}',
    nudge_sent BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(parent_id, date)
);

-- ═══════════════ INDEXES ═══════════════
CREATE INDEX idx_children_family ON children(family_id);
CREATE INDEX idx_children_phone ON children(phone);
CREATE INDEX idx_parents_family ON parents(family_id);
CREATE INDEX idx_parents_phone ON parents(phone);
CREATE INDEX idx_med_groups_parent ON medicine_groups(parent_id, sort_order);
CREATE INDEX idx_medicines_group ON medicines(group_id);
CREATE INDEX idx_variations_parent_tp ON message_variations(parent_id, touchpoint, is_selected);
CREATE INDEX idx_checkins_parent_date ON check_ins(parent_id, date);
CREATE INDEX idx_checkins_status ON check_ins(status) WHERE status = 'sent';
CREATE INDEX idx_health_flows_active ON health_flows(parent_id, state) WHERE state != 'resolved';
CREATE INDEX idx_alerts_family ON alerts(family_id, created_at DESC);
CREATE INDEX idx_concerns_parent ON concern_log(parent_id, is_resolved) WHERE is_resolved = FALSE;
CREATE INDEX idx_conv_state_parent ON conversation_state(parent_id, date);
CREATE INDEX idx_special_dates_parent ON special_dates(parent_id, date_value);

-- ═══════════════ RLS (service key bypasses, but good practice) ═══════════════
ALTER TABLE families ENABLE ROW LEVEL SECURITY;
ALTER TABLE children ENABLE ROW LEVEL SECURITY;
ALTER TABLE parents ENABLE ROW LEVEL SECURITY;
ALTER TABLE medicine_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE medicines ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_variations ENABLE ROW LEVEL SECURITY;
ALTER TABLE check_ins ENABLE ROW LEVEL SECURITY;
ALTER TABLE health_flows ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE concern_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE special_dates ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_state ENABLE ROW LEVEL SECURITY;

-- Service role full access policies
CREATE POLICY "service_all" ON families FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON children FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON parents FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON medicine_groups FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON medicines FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON message_variations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON check_ins FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON health_flows FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON alerts FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON concern_log FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON special_dates FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON conversation_state FOR ALL USING (true) WITH CHECK (true);
