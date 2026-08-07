import sys
import os
import duckdb
import numpy as np
import ruptures as rpt
import matplotlib.pyplot as plt

# Ensure scratch directory exists
os.makedirs(".gemini/scratch", exist_ok=True)

# Connect to db
con = duckdb.connect("data/processed/nhs.duckdb")

# Query variances
query = """
SELECT org_code, var_pop(breach_pct) as variance, count(breach_pct) as n_months
FROM fact_ae_attendances
WHERE org_code NOT IN ('R1H', 'R0A')
GROUP BY org_code
HAVING count(breach_pct) >= 10 AND var_pop(breach_pct) > 0.1
ORDER BY variance
"""
df_var = con.execute(query).df()

# Get 4 low variance (< 4) and 4 high variance (> 7)
low_var = df_var[df_var['variance'] < 4]['org_code'].head(4).tolist()
high_var = df_var[df_var['variance'] > 7]['org_code'].head(4).tolist()

trusts = low_var + high_var
print(f"Selected trusts: {trusts}")

# Fetch data for these trusts
query_data = f"""
SELECT org_code, month_date, breach_pct
FROM fact_ae_attendances
WHERE org_code IN {tuple(trusts)}
ORDER BY org_code, month_date
"""
df_data = con.execute(query_data).df()

for i, trust in enumerate(trusts):
    trust_data = df_data[df_data['org_code'] == trust]
    values = trust_data['breach_pct'].to_numpy(dtype=float)
    dates = trust_data['month_date'].tolist()
    
    # Method 1: L2 + BIC
    var = np.var(values)
    pen_bic = var * np.log(len(values))
    algo_l2 = rpt.Pelt(model="l2", min_size=3, jump=1).fit(values.reshape(-1, 1))
    bkps_l2 = algo_l2.predict(pen=pen_bic)
    bkps_l2 = bkps_l2[:-1]  # remove last point
    
    # Method 2: RBF + 1.2
    algo_rbf = rpt.Pelt(model="rbf", min_size=3, jump=1).fit(values.reshape(-1, 1))
    bkps_rbf = algo_rbf.predict(pen=1.2)
    bkps_rbf = bkps_rbf[:-1]
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    fig.suptitle(f"Trust: {trust} (Variance: {var:.2f})")
    
    ax1.plot(values, color='black', marker='.')
    for b in bkps_l2:
        ax1.axvline(x=b, color='red', linestyle='--')
    ax1.set_title(f"L2 + BIC (n={len(bkps_l2)})")
    
    ax2.plot(values, color='black', marker='.')
    for b in bkps_rbf:
        ax2.axvline(x=b, color='blue', linestyle='--')
    ax2.set_title(f"RBF + 1.2 (n={len(bkps_rbf)})")
    
    os.makedirs("reports/plots", exist_ok=True)
    plt.tight_layout()
    plt.savefig(f"reports/plots/trust_{trust}.png")
    plt.close()

print("Plots saved.")
