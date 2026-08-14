import re
import sys

with open('bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove matplotlib imports
content = re.sub(r'import matplotlib\nmatplotlib\.use\(\'Agg\'\).*?\nimport matplotlib\.pyplot as plt\n', '', content, flags=re.DOTALL)
content = re.sub(r'import matplotlib\.dates as mdates\n', '', content)
content = re.sub(r'from io import BytesIO\n', '', content)
# Ensure urllib.parse.quote is imported
if 'from urllib.parse import quote' not in content:
    content = content.replace('from urllib.parse import parse_qsl, urlparse, unquote', 'from urllib.parse import parse_qsl, urlparse, unquote, quote')


# 2. Replace get_crypto_chart_image
old_crypto = """        timestamps = [p[0] for p in raw_prices]
        prices = [p[1] for p in raw_prices]
        dates = [datetime.fromtimestamp(ts / 1000) for ts in timestamps]

        fig, ax = plt.subplots(figsize=(6, 3.8))
        fig.patch.set_facecolor('#0e1117')
        ax.set_facecolor('#0e1117')
        ax.plot(dates, prices, color='#00cc96', linewidth=1.5)
        ax.grid(True, color='gray', linestyle='--', linewidth=0.3, alpha=0.3)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#555')
        ax.spines['bottom'].set_color('#555')
        ax.tick_params(axis='x', colors='#aaa', labelsize=8, rotation=30)
        ax.tick_params(axis='y', colors='#aaa', labelsize=8)
        ax.set_title(f"{crypto_id.upper()} - {days}d", color='#ccc', fontsize=11, pad=8)
        ax.set_ylabel("USD", color='#aaa', fontsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))

        buf = BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=150,
                    facecolor=fig.get_facecolor(), edgecolor='none')
        buf.seek(0)
        plt.close(fig)
        result = buf.getvalue()"""

new_crypto = """        timestamps = [p[0] for p in raw_prices]
        prices = [p[1] for p in raw_prices]
        
        # Downsample to ~100 points for the URL API to keep it light
        step = max(1, len(prices) // 100)
        sliced_prices = prices[::step]
        sliced_dates = [datetime.fromtimestamp(ts / 1000).strftime('%b %d') for ts in timestamps[::step]]
        
        chart_config = {
            "type": "line",
            "data": {
                "labels": sliced_dates,
                "datasets": [{
                    "label": f"{crypto_id.upper()} USD",
                    "data": sliced_prices,
                    "borderColor": "#00cc96",
                    "borderWidth": 2,
                    "fill": False,
                    "pointRadius": 0
                }]
            },
            "options": {
                "legend": {"display": False},
                "title": {"display": True, "text": f"{crypto_id.upper()} - {days}d", "fontColor": "#ccc", "fontSize": 16},
                "scales": {
                    "xAxes": [{"gridLines": {"color": "#333", "zeroLineColor": "#555"}, "ticks": {"fontColor": "#aaa", "maxTicksLimit": 10}}],
                    "yAxes": [{"gridLines": {"color": "#333", "zeroLineColor": "#555"}, "ticks": {"fontColor": "#aaa"}}]
                },
                "layout": {"padding": 10}
            }
        }
        
        qc_url = f"https://quickchart.io/chart?c={quote(json.dumps(chart_config))}&w=600&h=380&bkg=0e1117"
        resp = session.get(qc_url, timeout=15)
        if resp.status_code != 200:
            raise ValueError(f"QuickChart API failed: {resp.status_code}")
        
        result = resp.content"""

content = content.replace(old_crypto, new_crypto)

# 3. Replace get_portfolio_chart_image
old_portfolio = """    fig, ax = plt.subplots(figsize=(7, 7))
    fig.patch.set_facecolor('#0e1117')
    ax.set_facecolor('#0e1117')

    wedge_colours = [colours_pool[i % len(colours_pool)] for i in range(len(labels))]
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=wedge_colours,
        autopct='%1.1f%%', startangle=140,
        textprops={'color': 'white', 'fontsize': 11},
        wedgeprops={'linewidth': 1.5, 'edgecolor': '#0e1117'}
    )
    for at in autotexts:
        at.set_fontsize(9)
        at.set_color('white')

    total = sum(sizes)
    ax.set_title(
        f"Portfolio Breakdown  (Total: ${total:,.2f})",
        color='white', fontsize=13, pad=20
    )
    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', dpi=120,
                facecolor=fig.get_facecolor(), edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()"""

new_portfolio = """    wedge_colours = [colours_pool[i % len(colours_pool)] for i in range(len(labels))]
    total = sum(sizes)
    
    chart_config = {
        "type": "outlabeledPie",
        "data": {
            "labels": labels,
            "datasets": [{
                "backgroundColor": wedge_colours,
                "data": sizes
            }]
        },
        "options": {
            "title": {"display": True, "text": f"Portfolio Breakdown (Total: ${total:,.2f})", "fontColor": "#fff", "fontSize": 20},
            "plugins": {
                "legend": False,
                "outlabels": {
                    "text": "%l %p",
                    "color": "white",
                    "stretch": 35,
                    "font": {
                        "resizable": True,
                        "minSize": 12,
                        "maxSize": 18
                    }
                }
            }
        }
    }
    
    qc_url = f"https://quickchart.io/chart?c={quote(json.dumps(chart_config))}&w=700&h=700&bkg=0e1117"
    resp = session.get(qc_url, timeout=15)
    if resp.status_code != 200:
        raise ValueError(f"QuickChart API failed: {resp.status_code}")
    
    return resp.content"""

content = content.replace(old_portfolio, new_portfolio)

with open('bot.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Replacement script complete")
