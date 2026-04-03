-- ═══════════════════════════════════════════════════════════════════════════════
-- AYANA Care Companion — Complete Supabase Setup
-- 
-- ONE script: drops all → creates all → seeds variants → storage bucket
-- Run this ONCE in Supabase SQL Editor to set up everything from scratch.
-- 
-- WARNING: This DROPS all existing AYANA tables and data.
-- If you have live data, back it up first.
-- ═══════════════════════════════════════════════════════════════════════════════


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 1: CLEAN SLATE — Drop everything in reverse dependency order          ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

DROP TABLE IF EXISTS letters CASCADE;
DROP TABLE IF EXISTS conversation_state CASCADE;
DROP TABLE IF EXISTS special_dates CASCADE;
DROP TABLE IF EXISTS concern_log CASCADE;
DROP TABLE IF EXISTS alerts CASCADE;
DROP TABLE IF EXISTS health_flows CASCADE;
DROP TABLE IF EXISTS check_ins CASCADE;
DROP TABLE IF EXISTS message_variations CASCADE;
DROP TABLE IF EXISTS medicines CASCADE;
DROP TABLE IF EXISTS medicine_groups CASCADE;
DROP TABLE IF EXISTS parents CASCADE;
DROP TABLE IF EXISTS children CASCADE;
DROP TABLE IF EXISTS families CASCADE;


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 2: CREATE ALL TABLES                                                  ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ─── FAMILIES ────────────────────────────────────────────────────────────────
-- One row per subscribing family. Children and parents belong to a family.

CREATE TABLE families (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    plan                     TEXT DEFAULT 'trial'
                             CHECK (plan IN ('trial', 'active', 'expired', 'free')),
    trial_ends_at            TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '14 days'),
    report_format            TEXT DEFAULT 'combined'
                             CHECK (report_format IN ('combined', 'separate')),
    backup_contact           TEXT,          -- E.164 phone for emergency escalation
    razorpay_subscription_id TEXT,
    stripe_customer_id       TEXT,
    created_at               TIMESTAMPTZ DEFAULT NOW()
);


-- ─── CHILDREN ────────────────────────────────────────────────────────────────
-- Caregivers (children/siblings) who interact with AYANA via commands.

CREATE TABLE children (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id   UUID REFERENCES families(id) ON DELETE CASCADE,
    phone       TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    report_time TIME DEFAULT '20:00',
    timezone    TEXT DEFAULT 'Asia/Kolkata',
    is_primary  BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);


-- ─── PARENTS ─────────────────────────────────────────────────────────────────
-- Elderly parents who receive daily check-in messages.

CREATE TABLE parents (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id        UUID REFERENCES families(id) ON DELETE CASCADE,
    phone            TEXT UNIQUE NOT NULL,
    name             TEXT NOT NULL,
    nickname         TEXT NOT NULL,
    language         TEXT DEFAULT 'te',
    tts_voice        TEXT DEFAULT 'roopa',
    checkin_time     TIME DEFAULT '08:00',
    routine          JSONB DEFAULT '{}',        -- wake_time, notes, travel_mode, etc.
    activities       JSONB DEFAULT '[]',        -- ["morning walk", "temple", "garden"]
    conditions       JSONB DEFAULT '[]',        -- ["BP", "diabetes"]
    alone_during_day BOOLEAN DEFAULT FALSE,
    training_day     INTEGER DEFAULT 0,
    is_active        BOOLEAN DEFAULT TRUE,
    paused_until     DATE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);


-- ─── MEDICINE GROUPS ─────────────────────────────────────────────────────────
-- Groups medicines by timing (before food, after food, night, etc.)

CREATE TABLE medicine_groups (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id    UUID REFERENCES parents(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    anchor_event TEXT NOT NULL
                 CHECK (anchor_event IN (
                     'wake', 'before_food', 'after_food', 'afternoon',
                     'evening', 'dinner', 'after_dinner', 'night'
                 )),
    time_window  TIME NOT NULL,
    sort_order   INTEGER DEFAULT 0
);


-- ─── MEDICINES ───────────────────────────────────────────────────────────────
-- Individual medicines within a group.

CREATE TABLE medicines (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    group_id        UUID REFERENCES medicine_groups(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    instructions    TEXT,
    is_as_needed    BOOLEAN DEFAULT FALSE,
    trigger_symptom TEXT
);


-- ─── MESSAGE VARIATIONS ──────────────────────────────────────────────────────
-- Rotating message templates per touchpoint. parent_id=NULL = global defaults.
-- All stored in CLEAN ENGLISH → Sarvam translates at runtime to any language.

CREATE TABLE message_variations (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id       UUID REFERENCES parents(id) ON DELETE CASCADE,
    touchpoint      TEXT NOT NULL
                    CHECK (touchpoint IN (
                        'morning_greeting', 'food_check', 'medicine_before_food',
                        'medicine_after_food', 'medicine_night', 'activity_check',
                        'evening_checkin', 'anything_else', 'goodnight'
                    )),
    message_text    TEXT NOT NULL,
    is_ai_generated BOOLEAN DEFAULT TRUE,
    is_selected     BOOLEAN DEFAULT TRUE,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ─── CHECK-INS ───────────────────────────────────────────────────────────────
-- One row per touchpoint per day per parent. Core data for reports.

CREATE TABLE check_ins (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id     UUID REFERENCES parents(id) ON DELETE CASCADE,
    date          DATE NOT NULL DEFAULT CURRENT_DATE,
    touchpoint    TEXT NOT NULL,
    status        TEXT DEFAULT 'sent'
                  CHECK (status IN ('sent', 'replied', 'missed', 'skipped')),
    mood          TEXT,
    raw_reply     TEXT,
    raw_audio_url TEXT,
    concerns      JSONB DEFAULT '[]',
    medicine_taken JSONB DEFAULT '{}',
    ai_extraction JSONB DEFAULT '{}',
    sent_at       TIMESTAMPTZ,
    replied_at    TIMESTAMPTZ,
    variation_id  UUID REFERENCES message_variations(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(parent_id, date, touchpoint)
);


-- ─── HEALTH FLOWS ────────────────────────────────────────────────────────────
-- Tracks ongoing health issues: active → recovery → confirmation → resolved

CREATE TABLE health_flows (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id   UUID REFERENCES parents(id) ON DELETE CASCADE,
    condition   TEXT NOT NULL,
    state       TEXT DEFAULT 'active'
                CHECK (state IN ('active', 'recovery', 'confirmation', 'resolved')),
    details     JSONB DEFAULT '{}',
    started_at  TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);


-- ─── ALERTS ──────────────────────────────────────────────────────────────────
-- Every emergency, missed check-in, concern pattern, etc.

CREATE TABLE alerts (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id      UUID REFERENCES families(id) ON DELETE CASCADE,
    parent_id      UUID REFERENCES parents(id),
    type           TEXT NOT NULL
                   CHECK (type IN (
                       'emergency', 'missed_checkin', 'concern_pattern',
                       'severe_pain', 'missed_streak', 'no_food'
                   )),
    message        TEXT NOT NULL,
    context        JSONB DEFAULT '{}',
    sent_to        JSONB DEFAULT '[]',
    call_attempted BOOLEAN DEFAULT FALSE,
    call_answered  BOOLEAN DEFAULT FALSE,
    acknowledged   BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);


-- ─── CONCERN LOG ─────────────────────────────────────────────────────────────
-- Tracks repeated concerns for pattern detection (knee pain 3x this week, etc.)

CREATE TABLE concern_log (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id    UUID REFERENCES parents(id) ON DELETE CASCADE,
    concern_text TEXT NOT NULL,
    category     TEXT,
    severity     TEXT CHECK (severity IN ('mild', 'moderate', 'severe')),
    first_seen   DATE DEFAULT CURRENT_DATE,
    last_seen    DATE DEFAULT CURRENT_DATE,
    frequency    INTEGER DEFAULT 1,
    is_resolved  BOOLEAN DEFAULT FALSE
);


-- ─── SPECIAL DATES ───────────────────────────────────────────────────────────
-- Birthdays, anniversaries, festivals — trigger special messages.

CREATE TABLE special_dates (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id  UUID REFERENCES parents(id) ON DELETE CASCADE,
    date_type  TEXT NOT NULL
               CHECK (date_type IN ('birthday', 'anniversary', 'festival', 'custom')),
    label      TEXT NOT NULL,
    date_value DATE NOT NULL,
    recurring  BOOLEAN DEFAULT TRUE
);


-- ─── CONVERSATION STATE ──────────────────────────────────────────────────────
-- Tracks where each parent is in today's conversation flow.

CREATE TABLE conversation_state (
    id                     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id              UUID REFERENCES parents(id) ON DELETE CASCADE,
    date                   DATE NOT NULL DEFAULT CURRENT_DATE,
    current_touchpoint     TEXT,
    awaiting_response      BOOLEAN DEFAULT FALSE,
    touchpoints_completed  JSONB DEFAULT '[]',
    touchpoints_remaining  JSONB DEFAULT '[]',
    context                JSONB DEFAULT '{}',
    pending_buttons        JSONB DEFAULT '[]',
    nudge_sent             BOOLEAN DEFAULT FALSE,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(parent_id, date)
);


-- ─── LETTERS / NOTES ─────────────────────────────────────────────────────────
-- Children write letters/notes for parents, delivered on schedule or immediately.

CREATE TABLE letters (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id      UUID REFERENCES families(id) ON DELETE CASCADE,
    from_child_id  UUID REFERENCES children(id),
    to_parent_id   UUID REFERENCES parents(id) ON DELETE CASCADE,
    content        TEXT NOT NULL,
    letter_type    TEXT DEFAULT 'letter'
                   CHECK (letter_type IN ('letter', 'note')),
    deliver_date   DATE,
    deliver_slot   TEXT DEFAULT 'morning_greeting',
    status         TEXT DEFAULT 'pending'
                   CHECK (status IN ('pending', 'delivered', 'failed')),
    delivered_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 3: INDEXES                                                            ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

CREATE INDEX idx_children_family    ON children(family_id);
CREATE INDEX idx_children_phone     ON children(phone);
CREATE INDEX idx_parents_family     ON parents(family_id);
CREATE INDEX idx_parents_phone      ON parents(phone);
CREATE INDEX idx_med_groups_parent  ON medicine_groups(parent_id, sort_order);
CREATE INDEX idx_medicines_group    ON medicines(group_id);
CREATE INDEX idx_variations_parent  ON message_variations(parent_id, touchpoint, is_selected);
CREATE INDEX idx_checkins_parent    ON check_ins(parent_id, date);
CREATE INDEX idx_checkins_sent      ON check_ins(status) WHERE status = 'sent';
CREATE INDEX idx_health_active      ON health_flows(parent_id, state) WHERE state != 'resolved';
CREATE INDEX idx_alerts_family      ON alerts(family_id, created_at DESC);
CREATE INDEX idx_concerns_parent    ON concern_log(parent_id, is_resolved) WHERE is_resolved = FALSE;
CREATE INDEX idx_conv_state         ON conversation_state(parent_id, date);
CREATE INDEX idx_special_dates      ON special_dates(parent_id, date_value);
CREATE INDEX idx_letters_pending    ON letters(deliver_date, status) WHERE status = 'pending';
CREATE INDEX idx_letters_parent     ON letters(to_parent_id);
CREATE INDEX idx_letters_family     ON letters(family_id);


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 4: ROW LEVEL SECURITY                                                 ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

-- Enable RLS on all tables (service key bypasses, but good practice)
ALTER TABLE families           ENABLE ROW LEVEL SECURITY;
ALTER TABLE children           ENABLE ROW LEVEL SECURITY;
ALTER TABLE parents            ENABLE ROW LEVEL SECURITY;
ALTER TABLE medicine_groups    ENABLE ROW LEVEL SECURITY;
ALTER TABLE medicines          ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_variations ENABLE ROW LEVEL SECURITY;
ALTER TABLE check_ins          ENABLE ROW LEVEL SECURITY;
ALTER TABLE health_flows       ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE concern_log        ENABLE ROW LEVEL SECURITY;
ALTER TABLE special_dates      ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE letters            ENABLE ROW LEVEL SECURITY;

-- Service role: full access on all tables
CREATE POLICY "service_all" ON families           FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON children           FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON parents            FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON medicine_groups    FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON medicines          FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON message_variations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON check_ins          FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON health_flows       FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON alerts             FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON concern_log        FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON special_dates      FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON conversation_state FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON letters            FOR ALL USING (true) WITH CHECK (true);


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 5: STORAGE BUCKET — TTS Audio Cache                                   ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

-- Create audio_cache bucket (public — WhatsApp needs direct URL access)
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'audio_cache',
    'audio_cache',
    true,
    5242880,  -- 5MB max per file
    ARRAY['audio/wav', 'audio/ogg', 'audio/mpeg']::text[]
)
ON CONFLICT (id) DO NOTHING;

-- Public read (WhatsApp fetches audio URL directly)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'public_audio_read' AND tablename = 'objects'
    ) THEN
        CREATE POLICY "public_audio_read" ON storage.objects
            FOR SELECT USING (bucket_id = 'audio_cache');
    END IF;
END $$;

-- Service role upload + update
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'service_audio_upload' AND tablename = 'objects'
    ) THEN
        CREATE POLICY "service_audio_upload" ON storage.objects
            FOR INSERT WITH CHECK (bucket_id = 'audio_cache');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'service_audio_update' AND tablename = 'objects'
    ) THEN
        CREATE POLICY "service_audio_update" ON storage.objects
            FOR UPDATE USING (bucket_id = 'audio_cache');
    END IF;
END $$;


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  STEP 6: SEED MESSAGE VARIANTS (Global Defaults)                            ║
-- ║                                                                              ║
-- ║  All variants in CLEAN ENGLISH → Sarvam translates at runtime               ║
-- ║  to any of 10 languages (Te, Hi, Ta, Kn, Ml, Bn, Mr, Gu, Pa, En).          ║
-- ║  NO romanized regional words — they break the translator.                   ║
-- ║  parent_id = NULL → global fallbacks when Gemini hasn't generated            ║
-- ║  parent-specific variations yet.                                             ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

-- ═══════════════ MORNING GREETING (7 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'morning_greeting', 'Good morning {nickname}! Did you sleep well? How are you feeling today?', FALSE, TRUE),
(NULL, 'morning_greeting', '{nickname}... good morning! How are you doing today? Hope you rested well.', FALSE, TRUE),
(NULL, 'morning_greeting', 'Rise and shine, {nickname}! How is your health this morning?', FALSE, TRUE),
(NULL, 'morning_greeting', 'Good morning {nickname}! A new day has started... how do you feel?', FALSE, TRUE),
(NULL, 'morning_greeting', '{nickname}, good morning! Did you wake up fresh today? Everything okay?', FALSE, TRUE),
(NULL, 'morning_greeting', 'Hello {nickname}! Hope you had a peaceful night. How are you this morning?', FALSE, TRUE),
(NULL, 'morning_greeting', '{nickname}... it is a beautiful morning! Tell me, how are you feeling?', FALSE, TRUE);

-- ═══════════════ FOOD CHECK (7 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'food_check', '{nickname}... did you eat your breakfast? What did you have today?', FALSE, TRUE),
(NULL, 'food_check', 'Time for a meal check, {nickname}! Did you eat something nice?', FALSE, TRUE),
(NULL, 'food_check', '{nickname}, did you have your food? Please do not skip your meals!', FALSE, TRUE),
(NULL, 'food_check', 'Have you eaten, {nickname}? Good food keeps you strong and healthy.', FALSE, TRUE),
(NULL, 'food_check', '{nickname}... did you have your meal on time today? What did you cook?', FALSE, TRUE),
(NULL, 'food_check', '{nickname}, just checking... breakfast done? I hope it was tasty!', FALSE, TRUE),
(NULL, 'food_check', 'Did you eat well today, {nickname}? Your family wants you to eat properly.', FALSE, TRUE);

-- ═══════════════ MEDICINE BEFORE FOOD (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'medicine_before_food', '{nickname}... time for your morning medicine! Please take it before food.', FALSE, TRUE),
(NULL, 'medicine_before_food', 'Good morning {nickname}! Do not forget your empty stomach medicine.', FALSE, TRUE),
(NULL, 'medicine_before_food', '{nickname}, your morning tablet time! Take it before breakfast.', FALSE, TRUE),
(NULL, 'medicine_before_food', 'Medicine reminder for {nickname}! Please take your before-food tablet.', FALSE, TRUE),
(NULL, 'medicine_before_food', '{nickname}... tablet time! Take your medicine before eating anything.', FALSE, TRUE);

-- ═══════════════ MEDICINE AFTER FOOD (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'medicine_after_food', '{nickname}... did you take your after-food medicine?', FALSE, TRUE),
(NULL, 'medicine_after_food', 'After your meal, {nickname} — time for your tablet!', FALSE, TRUE),
(NULL, 'medicine_after_food', '{nickname}, did you take your medicine after eating?', FALSE, TRUE),
(NULL, 'medicine_after_food', 'Medicine check! {nickname}, please take your after-meal tablet.', FALSE, TRUE),
(NULL, 'medicine_after_food', '{nickname}... finished eating? Now please take your tablet.', FALSE, TRUE);

-- ═══════════════ MEDICINE NIGHT (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'medicine_night', '{nickname}... night medicine time! Take it before sleeping.', FALSE, TRUE),
(NULL, 'medicine_night', 'Before bed, {nickname} — do not forget your night tablet!', FALSE, TRUE),
(NULL, 'medicine_night', '{nickname}, did you take your night medicine? Almost bedtime!', FALSE, TRUE),
(NULL, 'medicine_night', 'Night medicine reminder for {nickname}! Take it and rest well.', FALSE, TRUE),
(NULL, 'medicine_night', '{nickname}... did you take your night tablet? Take it and sleep peacefully.', FALSE, TRUE);

-- ═══════════════ ACTIVITY CHECK (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'activity_check', '{nickname}... what have you been up to today? Did you go for a walk?', FALSE, TRUE),
(NULL, 'activity_check', 'How is your day going, {nickname}? Did you do any of your favourite activities?', FALSE, TRUE),
(NULL, 'activity_check', '{nickname}, staying active? Tell me what you did today!', FALSE, TRUE),
(NULL, 'activity_check', 'What did you do today, {nickname}? Temple? Garden? Walk?', FALSE, TRUE),
(NULL, 'activity_check', '{nickname}... how was your day so far? Did you do something fun?', FALSE, TRUE);

-- ═══════════════ EVENING CHECKIN (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'evening_checkin', '{nickname}... how was your day? Everything okay this evening?', FALSE, TRUE),
(NULL, 'evening_checkin', 'Evening check-in, {nickname}! How are you feeling now?', FALSE, TRUE),
(NULL, 'evening_checkin', '{nickname}, hope you had a good day! How are you doing?', FALSE, TRUE),
(NULL, 'evening_checkin', 'Hello {nickname}! Day is almost over — how are you feeling tonight?', FALSE, TRUE),
(NULL, 'evening_checkin', '{nickname}... how is your evening going? All good?', FALSE, TRUE);

-- ═══════════════ ANYTHING ELSE (3 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'anything_else', '{nickname}... is there anything else you want to share? You can send a voice message too.', FALSE, TRUE),
(NULL, 'anything_else', 'Anything else on your mind, {nickname}? I am listening.', FALSE, TRUE),
(NULL, 'anything_else', '{nickname}, want to share anything else? Record a voice message if you prefer.', FALSE, TRUE);

-- ═══════════════ GOODNIGHT (5 variants) ═══════════════
INSERT INTO message_variations (parent_id, touchpoint, message_text, is_ai_generated, is_selected) VALUES
(NULL, 'goodnight', 'Good night {nickname}! Rest well... your family loves you.', FALSE, TRUE),
(NULL, 'goodnight', '{nickname}... good night! Sleep peacefully tonight. We love you.', FALSE, TRUE),
(NULL, 'goodnight', 'Time to rest, {nickname}. Good night and sweet dreams.', FALSE, TRUE),
(NULL, 'goodnight', '{nickname}... day is done! Rest well. Tomorrow is another beautiful day.', FALSE, TRUE),
(NULL, 'goodnight', 'Good night {nickname}! Sleep well and take care. Love you.', FALSE, TRUE);


-- ╔═══════════════════════════════════════════════════════════════════════════════╗
-- ║  DONE — Verify                                                              ║
-- ╚═══════════════════════════════════════════════════════════════════════════════╝

-- Quick sanity check — should return 13 tables
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_type = 'BASE TABLE'
ORDER BY table_name;

-- Should return 47 seeded variants
SELECT touchpoint, COUNT(*) as variants
FROM message_variations 
WHERE parent_id IS NULL
GROUP BY touchpoint
ORDER BY touchpoint;
