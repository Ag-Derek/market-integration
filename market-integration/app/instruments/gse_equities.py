"""
Every security listed on the Ghana Stock Exchange's equity market
(main board and Ghana Alternative Market), plus the one ETF.

Sources, checked 29-Sep-2026:
  * GSE listed-companies page (gse.com.gh/listed-companies/) -- 36 codes
    incl. the GLD ETF;
  * afx.kwayisi.org/gse/ daily price table -- adds AADS (AngloGold
    depositary shares) and the GAX names DIGICUT, HORDS, IIL, MMH and
    SAMBA, which the listed-companies page doesn't show.

Re-verify against the GSE before relying on this for anything real:
codes change on listings, delistings and renames.
"""

from app.models.instrument import Instrument

GSE_INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("AADS", "AngloGold Ashanti Depositary Shares", "Mining", kind="depositary"),
    Instrument("ACCESS", "Access Bank Ghana Plc", "Banking"),
    Instrument("ADB", "Agricultural Development Bank Plc", "Banking"),
    Instrument("AGA", "AngloGold Ashanti Plc", "Mining"),
    Instrument("ALLGH", "Atlantic Lithium Limited", "Mining"),
    Instrument("ALW", "Aluworks Limited", "Manufacturing"),
    Instrument("ASG", "Asante Gold Corporation", "Mining"),
    Instrument("BOPP", "Benso Oil Palm Plantation Ltd", "Agriculture"),
    Instrument("CAL", "CalBank PLC", "Banking"),
    Instrument("CLYD", "Clydestone (Ghana) Limited", "Technology"),
    Instrument("CMLT", "Camelot Ghana Ltd", "Printing & Publishing"),
    Instrument("CPC", "Cocoa Processing Company Ltd", "Agriculture"),
    Instrument("DASPHARMA", "Dannex Ayrton Starwin Plc", "Pharmaceuticals"),
    Instrument("DIGICUT", "Digicut Production & Advertising Ltd", "Media"),
    Instrument("EGH", "Ecobank Ghana PLC", "Banking"),
    Instrument("EGL", "Enterprise Group PLC", "Insurance"),
    Instrument("ETI", "Ecobank Transnational Incorporated", "Banking"),
    Instrument("FAB", "First Atlantic Bank PLC", "Banking"),
    Instrument("FML", "Fan Milk PLC", "Food & Beverage"),
    Instrument("GCB", "GCB Bank PLC", "Banking"),
    Instrument("GGBL", "Guinness Ghana Breweries PLC", "Food & Beverage"),
    Instrument("GLD", "NewGold Issuer Ltd (Gold ETF)", "Commodities", kind="etf"),
    Instrument("GOIL", "GOIL PLC", "Energy"),
    Instrument("HORDS", "Hords Limited", "Manufacturing"),
    Instrument("IIL", "Intravenous Infusions Limited", "Pharmaceuticals"),
    Instrument("KASA", "Kasapreko PLC", "Food & Beverage"),
    Instrument("MAC", "Mega African Capital Limited", "Financial Services"),
    Instrument("MMH", "Meridian-Marshall Holdings", "Financial Services"),
    Instrument("MTNGH", "Scancom PLC (MTN Ghana)", "Telecommunications"),
    Instrument("PBC", "Produce Buying Company Ltd", "Agriculture"),
    Instrument("RBGH", "Republic Bank (Ghana) PLC", "Banking"),
    Instrument("SAMBA", "Samba Foods Limited", "Food & Beverage"),
    Instrument("SCB", "Standard Chartered Bank Ghana PLC", "Banking"),
    Instrument("SCB-PREF", "Standard Chartered Bank Ghana PLC (Preference)", "Banking", kind="preference"),
    Instrument("SIC", "SIC Insurance Company Limited", "Insurance"),
    Instrument("SOGEGH", "Societe Generale Ghana PLC", "Banking"),
    Instrument("SWL", "Sam Wood Ltd", "Manufacturing"),
    Instrument("TBL", "Trust Bank Limited (The Gambia)", "Banking"),
    Instrument("TLW", "Tullow Oil Plc", "Energy"),
    Instrument("TOTAL", "TotalEnergies Marketing Ghana PLC", "Energy"),
    Instrument("UNIL", "Unilever Ghana PLC", "Consumer Goods"),
    Instrument("ZEN", "ZEN Petroleum Holdings PLC", "Energy"),
)
