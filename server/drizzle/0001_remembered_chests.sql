CREATE TABLE remembered_chests (
  point_id INTEGER PRIMARY KEY CHECK (point_id BETWEEN 0 AND 964),
  observations INTEGER NOT NULL CHECK (observations > 0),
  last_seen_hour INTEGER NOT NULL
);
--> statement-breakpoint
INSERT INTO remembered_chests(point_id, observations, last_seen_hour)
SELECT point_id, COUNT(*), MAX(hour_bucket)
FROM sightings
GROUP BY point_id;
