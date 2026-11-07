-- Demo legacy Oracle estate. Executed once by the gvenzl image (as SYS) on first start.
ALTER SESSION SET CONTAINER = FREEPDB1;

CREATE USER demo IDENTIFIED BY "Demo_dev_pw1" QUOTA UNLIMITED ON users;
GRANT CREATE SESSION TO demo;

CREATE TABLE demo.customers (
  customer_id NUMBER(10) PRIMARY KEY,
  name VARCHAR2(100) NOT NULL,
  email VARCHAR2(200),
  credit_limit NUMBER(12,2) DEFAULT 0 NOT NULL,
  created_at DATE DEFAULT SYSDATE NOT NULL
);
CREATE TABLE demo.orders (
  order_id NUMBER(10) PRIMARY KEY,
  customer_id NUMBER(10) NOT NULL REFERENCES demo.customers(customer_id),
  order_date DATE DEFAULT SYSDATE NOT NULL,
  status VARCHAR2(20) DEFAULT 'NEW' NOT NULL
);
CREATE TABLE demo.order_items (
  item_id NUMBER(10) PRIMARY KEY,
  order_id NUMBER(10) NOT NULL REFERENCES demo.orders(order_id),
  sku VARCHAR2(40) NOT NULL,
  qty NUMBER(6) NOT NULL,
  unit_price NUMBER(12,2) NOT NULL
);
CREATE TABLE demo.ledger (
  ledger_id NUMBER(10) PRIMARY KEY,
  customer_id NUMBER(10) NOT NULL,
  period DATE NOT NULL,
  amount NUMBER NOT NULL
);
CREATE TABLE demo.audit_log (
  audit_id NUMBER(10) PRIMARY KEY,
  table_name VARCHAR2(30),
  action VARCHAR2(10),
  key_value NUMBER,
  at TIMESTAMP DEFAULT SYSTIMESTAMP
);
CREATE SEQUENCE demo.seq_orders START WITH 1 INCREMENT BY 1;
CREATE SEQUENCE demo.seq_audit START WITH 1 INCREMENT BY 1;

CREATE OR REPLACE FUNCTION demo.fn_order_total(p_order_id NUMBER) RETURN NUMBER IS
  v_total NUMBER;
BEGIN
  SELECT NVL(SUM(qty * unit_price), 0) INTO v_total FROM demo.order_items WHERE order_id = p_order_id;
  RETURN v_total;
END;
/

CREATE OR REPLACE VIEW demo.vw_customer_orders AS
SELECT c.customer_id, c.name, o.order_id, o.order_date, demo.fn_order_total(o.order_id) AS total
FROM demo.customers c, demo.orders o
WHERE o.customer_id = c.customer_id(+)
/

CREATE OR REPLACE PACKAGE demo.pkg_billing AS
  PROCEDURE close_month(p_period DATE);
  PROCEDURE add_order(p_customer NUMBER, p_sku VARCHAR2, p_qty NUMBER, p_price NUMBER);
END pkg_billing;
/

CREATE OR REPLACE PACKAGE BODY demo.pkg_billing AS
  PROCEDURE add_order(p_customer NUMBER, p_sku VARCHAR2, p_qty NUMBER, p_price NUMBER) IS
    v_oid NUMBER;
  BEGIN
    v_oid := demo.seq_orders.NEXTVAL;
    INSERT INTO demo.orders (order_id, customer_id) VALUES (v_oid, p_customer);
    INSERT INTO demo.order_items (item_id, order_id, sku, qty, unit_price)
      VALUES (v_oid, v_oid, p_sku, p_qty, p_price);
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      ROLLBACK;
      RAISE;
  END add_order;

  PROCEDURE close_month(p_period DATE) IS
    CURSOR c IS SELECT customer_id, SUM(demo.fn_order_total(order_id)) amt
                FROM demo.orders WHERE order_date < ADD_MONTHS(p_period, 1) GROUP BY customer_id;
  BEGIN
    FOR r IN c LOOP
      INSERT INTO demo.ledger (ledger_id, customer_id, period, amount)
        VALUES (demo.seq_orders.NEXTVAL, r.customer_id, p_period, r.amt);
    END LOOP;
    EXECUTE IMMEDIATE 'UPDATE demo.orders SET status = ''CLOSED'' WHERE order_date < :1' USING p_period;
    COMMIT;
  END close_month;
END pkg_billing;
/

CREATE OR REPLACE TRIGGER demo.trg_orders_audit
AFTER INSERT OR UPDATE ON demo.orders
FOR EACH ROW
BEGIN
  INSERT INTO demo.audit_log (audit_id, table_name, action, key_value)
  VALUES (demo.seq_audit.NEXTVAL, 'ORDERS', 'UPSERT', :NEW.order_id);
END;
/

-- Read-only migration user: dictionary/code visibility only. No data access, no write rights.
CREATE USER mig_reader IDENTIFIED BY "Reader_dev_pw1";
GRANT CREATE SESSION TO mig_reader;
GRANT SELECT_CATALOG_ROLE TO mig_reader;
