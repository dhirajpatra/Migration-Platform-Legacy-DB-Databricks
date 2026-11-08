-- Demo legacy SQL Server estate. Run once by the sqlserver-init job as SA.
-- sqlcmd variables: $(DB) $(RUSER) $(RPASS)
IF DB_ID('$(DB)') IS NULL CREATE DATABASE [$(DB)];
GO
USE [$(DB)];
GO

CREATE TABLE dbo.Customers (
  CustomerId INT IDENTITY(1,1) PRIMARY KEY,
  Name NVARCHAR(100) NOT NULL,
  Email VARCHAR(200) NULL,
  CreditLimit MONEY NOT NULL DEFAULT 0,
  CreatedAt DATETIME NOT NULL DEFAULT GETDATE(),
  LastOrder DATETIME NULL
);
CREATE TABLE dbo.Orders (
  OrderId INT IDENTITY(1,1) PRIMARY KEY,
  CustomerId INT NOT NULL REFERENCES dbo.Customers(CustomerId),
  OrderDate DATETIME NOT NULL DEFAULT GETDATE(),
  Status VARCHAR(20) NOT NULL DEFAULT 'NEW',
  RowGuid UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID()
);
CREATE TABLE dbo.OrderItems (
  OrderItemId INT IDENTITY(1,1) PRIMARY KEY,
  OrderId INT NOT NULL REFERENCES dbo.Orders(OrderId),
  Sku VARCHAR(40) NOT NULL,
  Qty INT NOT NULL,
  UnitPrice DECIMAL(12,2) NOT NULL
);
CREATE TABLE dbo.Ledger (
  LedgerId INT IDENTITY(1,1) PRIMARY KEY,
  CustomerId INT NOT NULL,
  Period DATE NOT NULL,
  Amount DECIMAL(18,2) NOT NULL
);
CREATE TABLE dbo.AuditLog (
  AuditId INT IDENTITY(1,1) PRIMARY KEY,
  TableName SYSNAME NOT NULL,
  Action VARCHAR(10) NOT NULL,
  KeyValue INT NULL,
  At DATETIME NOT NULL DEFAULT GETDATE()
);
GO

CREATE FUNCTION dbo.fn_OrderTotal (@OrderId INT) RETURNS DECIMAL(18,2) AS
BEGIN
  DECLARE @t DECIMAL(18,2);
  SELECT @t = SUM(Qty * UnitPrice) FROM dbo.OrderItems WHERE OrderId = @OrderId;
  RETURN ISNULL(@t, 0);
END
GO

CREATE VIEW dbo.vw_CustomerOrders AS
SELECT c.CustomerId, c.Name, o.OrderId, o.OrderDate, dbo.fn_OrderTotal(o.OrderId) AS Total
FROM dbo.Customers c
JOIN dbo.Orders o WITH (NOLOCK) ON o.CustomerId = c.CustomerId;
GO

CREATE VIEW dbo.vw_OpenOrders AS
SELECT o.OrderId, c.Name, o.Status
FROM dbo.Orders o
JOIN dbo.Customers c ON c.CustomerId = o.CustomerId
WHERE o.Status = 'NEW';
GO

CREATE PROCEDURE dbo.usp_AddOrder @CustomerId INT, @Sku VARCHAR(40), @Qty INT, @Price DECIMAL(12,2) AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @oid INT;
  BEGIN TRY
    BEGIN TRAN;
    INSERT INTO dbo.Orders (CustomerId) VALUES (@CustomerId);
    SET @oid = SCOPE_IDENTITY();
    INSERT INTO dbo.OrderItems (OrderId, Sku, Qty, UnitPrice) VALUES (@oid, @Sku, @Qty, @Price);
    UPDATE dbo.Customers SET LastOrder = GETDATE() WHERE CustomerId = @CustomerId;
    COMMIT;
  END TRY
  BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK;
    THROW;
  END CATCH
END
GO

CREATE PROCEDURE dbo.usp_CloseMonth @Period DATE AS
BEGIN
  SET NOCOUNT ON;
  CREATE TABLE #tmp (CustomerId INT, Amt DECIMAL(18,2));
  INSERT INTO #tmp
    SELECT o.CustomerId, SUM(dbo.fn_OrderTotal(o.OrderId))
    FROM dbo.Orders o WHERE o.OrderDate < DATEADD(MONTH, 1, @Period) GROUP BY o.CustomerId;

  DECLARE @cid INT, @amt DECIMAL(18,2);
  DECLARE cur CURSOR FOR SELECT CustomerId, Amt FROM #tmp;
  OPEN cur; FETCH NEXT FROM cur INTO @cid, @amt;
  WHILE @@FETCH_STATUS = 0
  BEGIN
    INSERT INTO dbo.Ledger (CustomerId, Period, Amount) VALUES (@cid, @Period, @amt);
    FETCH NEXT FROM cur INTO @cid, @amt;
  END
  CLOSE cur; DEALLOCATE cur;

  DECLARE @sql NVARCHAR(400) = N'UPDATE dbo.Orders SET Status = ''CLOSED'' WHERE OrderDate < ''' + CONVERT(VARCHAR(10), @Period) + '''';
  EXEC (@sql);
  EXEC dbo.usp_AddAuditNote @Period;
END
GO

CREATE PROCEDURE dbo.usp_AddAuditNote @Period DATE AS
BEGIN
  INSERT INTO dbo.AuditLog (TableName, Action, KeyValue) VALUES ('Ledger', 'CLOSE', DATEPART(MONTH, @Period));
END
GO

CREATE TRIGGER dbo.trg_Orders_Audit ON dbo.Orders AFTER INSERT, UPDATE AS
BEGIN
  SET NOCOUNT ON;
  INSERT INTO dbo.AuditLog (TableName, Action, KeyValue)
  SELECT 'Orders', 'UPSERT', i.OrderId FROM inserted i;
END
GO

-- Read-only migration login: metadata + code visibility, NO data access, NO write rights
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '$(RUSER)')
  EXEC('CREATE LOGIN [$(RUSER)] WITH PASSWORD = ''$(RPASS)'', CHECK_POLICY = OFF');
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '$(RUSER)')
  EXEC('CREATE USER [$(RUSER)] FOR LOGIN [$(RUSER)]');
GO
GRANT VIEW DEFINITION TO [$(RUSER)];
GO
