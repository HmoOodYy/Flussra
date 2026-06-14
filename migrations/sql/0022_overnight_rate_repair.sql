-- Repair OVERNIGHT pay item: change RateBehavior from Fixed to PerUnit
-- and ensure PayItemRateTypeMap links it to the OVERNIGHT rate type.
UPDATE payroll.PayItems
SET RateBehavior = 'PerUnit', RequiresRate = TRUE
WHERE PayItemCode = 'OVERNIGHT'
  AND RateBehavior = 'Fixed';

INSERT INTO payroll.PayItemRateTypeMap (PayItemID, RateTypeID, IsPrimary, Status)
SELECT pi.PayItemID, rt.RateTypeID, TRUE, 'Active'
FROM payroll.PayItems pi
CROSS JOIN payroll.RateTypes rt
WHERE pi.PayItemCode = 'OVERNIGHT'
  AND rt.RateCode = 'OVERNIGHT'
  AND NOT EXISTS (
    SELECT 1 FROM payroll.PayItemRateTypeMap existing
    WHERE existing.PayItemID = pi.PayItemID
      AND existing.RateTypeID = rt.RateTypeID
  );
