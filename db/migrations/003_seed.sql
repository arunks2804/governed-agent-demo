-- 003: deterministic synthetic seed data. All names are fictional.
-- Acme: 60 orders (ids 1000-1059). Northstar: 45 orders (ids 2000-2044).

INSERT INTO app.orders (order_id, tenant_id, customer_name, amount, status, order_date)
SELECT
    999 + g,
    'acme',
    (ARRAY['Bluebell Bakery', 'Copperline Tools', 'Driftwood Cafe', 'Emberly Studio',
           'Foxglove Florist', 'Granite Peak Gear', 'Harborview Books', 'Ivy Lane Textiles',
           'Juniper Labs', 'Kestrel Bikes'])[1 + (g * 7) % 10],
    round((50 + ((g * 7919) % 4950) / 10.0)::numeric, 2),
    (ARRAY['pending', 'shipped', 'delivered', 'delivered', 'cancelled', 'refunded'])[1 + (g * 5) % 6],
    DATE '2025-01-01' + ((g * 37) % 540)
FROM generate_series(1, 60) AS g;

INSERT INTO app.orders (order_id, tenant_id, customer_name, amount, status, order_date)
SELECT
    1999 + g,
    'northstar',
    (ARRAY['Aurora Freight', 'Borealis Health', 'Cinder Robotics', 'Delta Marine Supply',
           'Echo Valley Farms', 'Fjord Analytics', 'Glacier Pharma', 'Halo Outfitters'])[1 + (g * 3) % 8],
    round((500 + ((g * 6271) % 19500) / 10.0)::numeric, 2),
    (ARRAY['pending', 'shipped', 'delivered', 'cancelled', 'refunded'])[1 + (g * 3) % 5],
    DATE '2025-01-01' + ((g * 53) % 540)
FROM generate_series(1, 45) AS g;

INSERT INTO app.billing_accounts (tenant_id, iban_synthetic, credit_limit) VALUES
    ('acme', 'XX00-SYNTHETIC-ACME-0001', 25000.00),
    ('northstar', 'XX00-SYNTHETIC-NSTR-0002', 90000.00);
