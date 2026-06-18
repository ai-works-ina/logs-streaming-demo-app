CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price NUMERIC(10,2) NOT NULL
);

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    item TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO products (name, price) VALUES
  ('widget', 9.99), ('gadget', 19.99), ('sprocket', 4.50),
  ('flange', 12.00), ('gizmo', 29.99), ('doohickey', 7.25);

INSERT INTO users (username) VALUES
  ('alice'), ('bob'), ('carol'), ('dave'), ('erin'), ('frank');
