CREATE TABLE sightings (
  point_id INTEGER NOT NULL CHECK (point_id BETWEEN 0 AND 964),
  hour_bucket INTEGER NOT NULL,
  PRIMARY KEY (point_id, hour_bucket)
);
--> statement-breakpoint
CREATE INDEX sightings_hour_bucket ON sightings(hour_bucket);
