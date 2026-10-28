-- Bounded, ordered Company Driver scans for Import name discovery.
CREATE INDEX ix_drivers_company_driverid
    ON core.Drivers (CompanyID, DriverID);
