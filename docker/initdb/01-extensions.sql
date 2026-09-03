-- Runs once when the db volume is first created.
--
-- postgis     : geography/geometry types, ST_DWithin for footpath generation (M4)
-- pg_trgm     : trigram index behind /stops/search autocomplete (M5)
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
