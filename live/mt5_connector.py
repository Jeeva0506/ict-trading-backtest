def export_spx500_cash_sessions(self):
        """Export fresh 15-min and 3-min SPX500 data to Excel."""
        # 15-min direct from MT5
        self.export_ohlc_to_excel(
            symbol="SPX500",
            timeframe=mt5.TIMEFRAME_M15,
            count=1000,
            filename="../data/raw/SPX500_1YEAR_M15.xlsx"
        )
        # 3-min: If MT5 supports, use direct; else, aggregate from M1
        if hasattr(mt5, 'TIMEFRAME_M3'):
            tf_m3 = mt5.TIMEFRAME_M3
        else:
            tf_m3 = 3  # Custom aggregation needed if not available
        self.export_ohlc_to_excel(
            symbol="SPX500",
            timeframe=tf_m3,
            count=1000,
            filename="../data/raw/SPX500_1YEAR_M3.xlsx"
        )
