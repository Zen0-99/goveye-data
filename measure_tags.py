import sqlite3, os

conn = sqlite3.connect('goveye.db')
tables = ['division_tags', 'bill_tags', 'tag_metadata', 'publication_tags',
          'statement_tags', 'legislation_tags', 'mp_tags', 'source_recommendations']

if os.path.exists('tag_tables_dump.db'):
    os.remove('tag_tables_dump.db')
dump = sqlite3.connect('tag_tables_dump.db')

for t in tables:
    try:
        rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        cols = [d[0] for d in conn.execute(f'SELECT * FROM {t} LIMIT 0').description]
        col_defs = ','.join(f'{c} TEXT' for c in cols)
        dump.execute(f'CREATE TABLE {t} ({col_defs})')
        dump.executemany(f'INSERT INTO {t} VALUES ({",".join("?"*len(cols))})', rows)
        dump.commit()
        print(f'{t}: {len(rows)} rows')
    except Exception as e:
        print(f'{t}: {e}')

dump.close()
conn.close()
print(f'tag_tables_dump.db size: {os.path.getsize("tag_tables_dump.db") / 1024:.0f} KB')
