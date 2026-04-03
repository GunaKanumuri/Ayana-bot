-- ═══════════════════════════════════════════════════════════
-- AYANA — Supabase Storage Setup
-- Run this in Supabase SQL Editor to create the audio cache bucket
-- This enables TTS audio to persist across Railway deploys
-- ═══════════════════════════════════════════════════════════

-- Create the audio_cache storage bucket (public for WhatsApp to access)
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'audio_cache',
    'audio_cache',
    true,
    5242880,  -- 5MB max per file
    ARRAY['audio/wav', 'audio/ogg', 'audio/mpeg']::text[]
)
ON CONFLICT (id) DO NOTHING;

-- Allow public read access (WhatsApp needs to fetch the audio URL)
CREATE POLICY "Public audio read" ON storage.objects
    FOR SELECT USING (bucket_id = 'audio_cache');

-- Allow service role to upload
CREATE POLICY "Service audio upload" ON storage.objects
    FOR INSERT WITH CHECK (bucket_id = 'audio_cache');

-- Allow service role to update (upsert)
CREATE POLICY "Service audio update" ON storage.objects
    FOR UPDATE USING (bucket_id = 'audio_cache');