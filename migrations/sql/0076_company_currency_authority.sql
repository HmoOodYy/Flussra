-- 0076: Company currency authority (P3a).
-- ISO 4217 List One - Current Currency & Funds; source: SIX, official
-- maintenance agency. Snapshot: 2026-10-02.
-- https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml
-- Current entries with numeric Minor Unit only; repeated entity rows are
-- collapsed after verifying identical name/numeric/minor metadata. XTS and
-- XXX are excluded. Historical List Three and BGN are not seeded.

-- Pre-production boundary: do not infer or backfill historical currency.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM payroll.driverrates)
       OR EXISTS (SELECT 1 FROM payroll.driverratetiers)
       OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents)
       OR EXISTS (SELECT 1 FROM payroll.driverpayrules)
       OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines
                  WHERE rateamount IS NOT NULL OR calculatedamount IS NOT NULL)
       OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots)
       OR EXISTS (SELECT 1 FROM payroll.payrollfinallines)
       OR EXISTS (SELECT 1 FROM payroll.payprofilerates)
    THEN
        RAISE EXCEPTION 'P3A_REQUIRES_CLEAN_PREPRODUCTION_MONETARY_STATE'
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

CREATE TABLE core.SupportedCurrencies (
    CurrencyCode VARCHAR(3) PRIMARY KEY,
    CurrencyName VARCHAR(160) NOT NULL,
    NumericCode CHAR(3) NOT NULL,
    MinorUnitDigits SMALLINT NOT NULL,
    CONSTRAINT ck_SupportedCurrencies_MinorUnitDigits
        CHECK (MinorUnitDigits BETWEEN 0 AND 4),
    CONSTRAINT ck_SupportedCurrencies_Code
        CHECK (CurrencyCode ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_SupportedCurrencies_Numeric
        CHECK (NumericCode ~ '^[0-9]{3}$')
);

INSERT INTO core.SupportedCurrencies
    (CurrencyCode, CurrencyName, NumericCode, MinorUnitDigits)
VALUES
    ('AED', 'UAE Dirham', '784', 2),
    ('AFN', 'Afghani', '971', 2),
    ('ALL', 'Lek', '008', 2),
    ('AMD', 'Armenian Dram', '051', 2),
    ('AOA', 'Kwanza', '973', 2),
    ('ARS', 'Argentine Peso', '032', 2),
    ('AUD', 'Australian Dollar', '036', 2),
    ('AWG', 'Aruban Florin', '533', 2),
    ('AZN', 'Azerbaijan Manat', '944', 2),
    ('BAM', 'Convertible Mark', '977', 2),
    ('BBD', 'Barbados Dollar', '052', 2),
    ('BDT', 'Taka', '050', 2),
    ('BHD', 'Bahraini Dinar', '048', 3),
    ('BIF', 'Burundi Franc', '108', 0),
    ('BMD', 'Bermudian Dollar', '060', 2),
    ('BND', 'Brunei Dollar', '096', 2),
    ('BOB', 'Boliviano', '068', 2),
    ('BOV', 'Mvdol', '984', 2),
    ('BRL', 'Brazilian Real', '986', 2),
    ('BSD', 'Bahamian Dollar', '044', 2),
    ('BTN', 'Ngultrum', '064', 2),
    ('BWP', 'Pula', '072', 2),
    ('BYN', 'Belarusian Ruble', '933', 2),
    ('BZD', 'Belize Dollar', '084', 2),
    ('CAD', 'Canadian Dollar', '124', 2),
    ('CDF', 'Congolese Franc', '976', 2),
    ('CHE', 'WIR Euro', '947', 2),
    ('CHF', 'Swiss Franc', '756', 2),
    ('CHW', 'WIR Franc', '948', 2),
    ('CLF', 'Unidad de Fomento', '990', 4),
    ('CLP', 'Chilean Peso', '152', 0),
    ('CNY', 'Yuan Renminbi', '156', 2),
    ('COP', 'Colombian Peso', '170', 2),
    ('COU', 'Unidad de Valor Real', '970', 2),
    ('CRC', 'Costa Rican Colon', '188', 2),
    ('CUP', 'Cuban Peso', '192', 2),
    ('CVE', 'Cabo Verde Escudo', '132', 2),
    ('CZK', 'Czech Koruna', '203', 2),
    ('DJF', 'Djibouti Franc', '262', 0),
    ('DKK', 'Danish Krone', '208', 2),
    ('DOP', 'Dominican Peso', '214', 2),
    ('DZD', 'Algerian Dinar', '012', 2),
    ('EGP', 'Egyptian Pound', '818', 2),
    ('ERN', 'Nakfa', '232', 2),
    ('ETB', 'Ethiopian Birr', '230', 2),
    ('EUR', 'Euro', '978', 2),
    ('FJD', 'Fiji Dollar', '242', 2),
    ('FKP', 'Falkland Islands Pound', '238', 2),
    ('GBP', 'Pound Sterling', '826', 2),
    ('GEL', 'Lari', '981', 2),
    ('GHS', 'Ghana Cedi', '936', 2),
    ('GIP', 'Gibraltar Pound', '292', 2),
    ('GMD', 'Dalasi', '270', 2),
    ('GNF', 'Guinean Franc', '324', 0),
    ('GTQ', 'Quetzal', '320', 2),
    ('GYD', 'Guyana Dollar', '328', 2),
    ('HKD', 'Hong Kong Dollar', '344', 2),
    ('HNL', 'Lempira', '340', 2),
    ('HTG', 'Gourde', '332', 2),
    ('HUF', 'Forint', '348', 2),
    ('IDR', 'Rupiah', '360', 2),
    ('ILS', 'New Israeli Sheqel', '376', 2),
    ('INR', 'Indian Rupee', '356', 2),
    ('IQD', 'Iraqi Dinar', '368', 3),
    ('IRR', 'Iranian Rial', '364', 2),
    ('ISK', 'Iceland Krona', '352', 0),
    ('JMD', 'Jamaican Dollar', '388', 2),
    ('JOD', 'Jordanian Dinar', '400', 3),
    ('JPY', 'Yen', '392', 0),
    ('KES', 'Kenyan Shilling', '404', 2),
    ('KGS', 'Som', '417', 2),
    ('KHR', 'Riel', '116', 2),
    ('KMF', 'Comorian Franc ', '174', 0),
    ('KPW', 'North Korean Won', '408', 2),
    ('KRW', 'Won', '410', 0),
    ('KWD', 'Kuwaiti Dinar', '414', 3),
    ('KYD', 'Cayman Islands Dollar', '136', 2),
    ('KZT', 'Tenge', '398', 2),
    ('LAK', 'Lao Kip', '418', 2),
    ('LBP', 'Lebanese Pound', '422', 2),
    ('LKR', 'Sri Lanka Rupee', '144', 2),
    ('LRD', 'Liberian Dollar', '430', 2),
    ('LSL', 'Loti', '426', 2),
    ('LYD', 'Libyan Dinar', '434', 3),
    ('MAD', 'Moroccan Dirham', '504', 2),
    ('MDL', 'Moldovan Leu', '498', 2),
    ('MGA', 'Malagasy Ariary', '969', 2),
    ('MKD', 'Denar', '807', 2),
    ('MMK', 'Kyat', '104', 2),
    ('MNT', 'Tugrik', '496', 2),
    ('MOP', 'Pataca', '446', 2),
    ('MRU', 'Ouguiya', '929', 2),
    ('MUR', 'Mauritius Rupee', '480', 2),
    ('MVR', 'Rufiyaa', '462', 2),
    ('MWK', 'Malawi Kwacha', '454', 2),
    ('MXN', 'Mexican Peso', '484', 2),
    ('MXV', 'Mexican Unidad de Inversion (UDI)', '979', 2),
    ('MYR', 'Malaysian Ringgit', '458', 2),
    ('MZN', 'Mozambique Metical', '943', 2),
    ('NAD', 'Namibia Dollar', '516', 2),
    ('NGN', 'Naira', '566', 2),
    ('NIO', 'Cordoba Oro', '558', 2),
    ('NOK', 'Norwegian Krone', '578', 2),
    ('NPR', 'Nepalese Rupee', '524', 2),
    ('NZD', 'New Zealand Dollar', '554', 2),
    ('OMR', 'Rial Omani', '512', 3),
    ('PAB', 'Balboa', '590', 2),
    ('PEN', 'Sol', '604', 2),
    ('PGK', 'Kina', '598', 2),
    ('PHP', 'Philippine Peso', '608', 2),
    ('PKR', 'Pakistan Rupee', '586', 2),
    ('PLN', 'Zloty', '985', 2),
    ('PYG', 'Guarani', '600', 0),
    ('QAR', 'Qatari Rial', '634', 2),
    ('RON', 'Romanian Leu', '946', 2),
    ('RSD', 'Serbian Dinar', '941', 2),
    ('RUB', 'Russian Ruble', '643', 2),
    ('RWF', 'Rwanda Franc', '646', 0),
    ('SAR', 'Saudi Riyal', '682', 2),
    ('SBD', 'Solomon Islands Dollar', '090', 2),
    ('SCR', 'Seychelles Rupee', '690', 2),
    ('SDG', 'Sudanese Pound', '938', 2),
    ('SEK', 'Swedish Krona', '752', 2),
    ('SGD', 'Singapore Dollar', '702', 2),
    ('SHP', 'Saint Helena Pound', '654', 2),
    ('SLE', 'Leone', '925', 2),
    ('SOS', 'Somali Shilling', '706', 2),
    ('SRD', 'Surinam Dollar', '968', 2),
    ('SSP', 'South Sudanese Pound', '728', 2),
    ('STN', 'Dobra', '930', 2),
    ('SVC', 'El Salvador Colon', '222', 2),
    ('SYP', 'Syrian Pound', '760', 2),
    ('SZL', 'Lilangeni', '748', 2),
    ('THB', 'Baht', '764', 2),
    ('TJS', 'Somoni', '972', 2),
    ('TMT', 'Turkmenistan New Manat', '934', 2),
    ('TND', 'Tunisian Dinar', '788', 3),
    ('TOP', 'Pa’anga', '776', 2),
    ('TRY', 'Turkish Lira', '949', 2),
    ('TTD', 'Trinidad and Tobago Dollar', '780', 2),
    ('TWD', 'New Taiwan Dollar', '901', 2),
    ('TZS', 'Tanzanian Shilling', '834', 2),
    ('UAH', 'Hryvnia', '980', 2),
    ('UGX', 'Uganda Shilling', '800', 0),
    ('USD', 'US Dollar', '840', 2),
    ('USN', 'US Dollar (Next day)', '997', 2),
    ('UYI', 'Uruguay Peso en Unidades Indexadas (UI)', '940', 0),
    ('UYU', 'Peso Uruguayo', '858', 2),
    ('UYW', 'Unidad Previsional', '927', 4),
    ('UZS', 'Uzbekistan Sum', '860', 2),
    ('VED', 'Bolívar Soberano', '926', 2),
    ('VES', 'Bolívar Soberano', '928', 2),
    ('VND', 'Dong', '704', 0),
    ('VUV', 'Vatu', '548', 0),
    ('WST', 'Tala', '882', 2),
    ('XAD', 'Arab Accounting Dinar', '396', 2),
    ('XAF', 'CFA Franc BEAC', '950', 0),
    ('XCD', 'East Caribbean Dollar', '951', 2),
    ('XCG', 'Caribbean Guilder', '532', 2),
    ('XOF', 'CFA Franc BCEAO', '952', 0),
    ('XPF', 'CFP Franc', '953', 0),
    ('YER', 'Yemeni Rial', '886', 2),
    ('ZAR', 'Rand', '710', 2),
    ('ZMW', 'Zambian Kwacha', '967', 2),
    ('ZWG', 'Zimbabwe Gold', '924', 2);

ALTER TABLE core.Companies
    ADD COLUMN CurrencyCode VARCHAR(3) NULL,
    ADD CONSTRAINT fk_Companies_Currency
        FOREIGN KEY (CurrencyCode)
        REFERENCES core.SupportedCurrencies(CurrencyCode);

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD COLUMN CurrencyCode VARCHAR(3) NOT NULL,
    ADD COLUMN CurrencyMinorUnitDigits SMALLINT NOT NULL,
    ADD CONSTRAINT fk_CalculationSnapshots_Currency
        FOREIGN KEY (CurrencyCode)
        REFERENCES core.SupportedCurrencies(CurrencyCode),
    ADD CONSTRAINT ck_CalculationSnapshots_CurrencyMinor
        CHECK (CurrencyMinorUnitDigits BETWEEN 0 AND 4);

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD COLUMN CurrencyCodeSnapshot VARCHAR(3) NOT NULL,
    ADD COLUMN CurrencyMinorUnitDigitsSnapshot SMALLINT NOT NULL,
    ADD CONSTRAINT fk_UsedRateDefinitions_Currency
        FOREIGN KEY (CurrencyCodeSnapshot)
        REFERENCES core.SupportedCurrencies(CurrencyCode),
    ADD CONSTRAINT ck_UsedRateDefinitions_CurrencyMinor
        CHECK (CurrencyMinorUnitDigitsSnapshot BETWEEN 0 AND 4);

ALTER TABLE payroll.PayrollCalculationSnapshotBonusEvents
    ADD COLUMN CurrencyCodeSnapshot VARCHAR(3) NOT NULL,
    ADD COLUMN CurrencyMinorUnitDigitsSnapshot SMALLINT NOT NULL,
    ADD CONSTRAINT fk_BonusEventEvidence_Currency
        FOREIGN KEY (CurrencyCodeSnapshot)
        REFERENCES core.SupportedCurrencies(CurrencyCode),
    ADD CONSTRAINT ck_BonusEventEvidence_CurrencyMinor
        CHECK (CurrencyMinorUnitDigitsSnapshot BETWEEN 0 AND 4);

ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN CurrencyCode VARCHAR(3) NOT NULL,
    ADD COLUMN CurrencyMinorUnitDigits SMALLINT NOT NULL,
    ADD CONSTRAINT fk_FinalLines_Currency
        FOREIGN KEY (CurrencyCode)
        REFERENCES core.SupportedCurrencies(CurrencyCode),
    ADD CONSTRAINT ck_FinalLines_CurrencyMinor
        CHECK (CurrencyMinorUnitDigits BETWEEN 0 AND 4);

CREATE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT
        EXISTS (SELECT 1 FROM payroll.driverrates r
                WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverratetiers t
                   JOIN payroll.driverrates r ON r.driverrateid = t.driverrateid
                   WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents b
                   WHERE b.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payprofilerates pr
                   JOIN payroll.payprofiles pp ON pp.payprofileid = pr.payprofileid
                   WHERE pp.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverpayrules p
                   WHERE p.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines d
                   WHERE d.companyid = p_company_id
                     AND (d.rateamount IS NOT NULL OR d.calculatedamount IS NOT NULL))
        OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots s
                   WHERE s.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollfinallines f
                   WHERE f.companyid = p_company_id);
$$;

CREATE FUNCTION core.fn_guard_company_currency_change()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.CurrencyCode IS NULL
       AND core.fn_company_has_durable_monetary_state(OLD.CompanyID) THEN
        RAISE EXCEPTION 'COMPANY_CURRENCY_INVARIANT_VIOLATION'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.CurrencyCode IS NULL THEN
        RAISE EXCEPTION 'COMPANY_CURRENCY_REQUIRED'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.CurrencyCode IS DISTINCT FROM OLD.CurrencyCode
       AND core.fn_company_has_durable_monetary_state(OLD.CompanyID) THEN
        RAISE EXCEPTION 'COMPANY_CURRENCY_CHANGE_BLOCKED'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_Companies_CurrencyChange
    BEFORE UPDATE OF CurrencyCode ON core.Companies
    FOR EACH ROW EXECUTE FUNCTION core.fn_guard_company_currency_change();
