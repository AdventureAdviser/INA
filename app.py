# app.py
# ──────────────────────────────────────────────────────────────
#  Full application file (Flask + SQLite) – Updated April 2025
#  Добавлены сохранение/загрузка планов питания, правки профиля, основные фичи
# ──────────────────────────────────────────────────────────────

import os
import sqlite3
import requests
from requests.exceptions import RequestException
from flask import (
    Flask, render_template, request, redirect,
    url_for, g, session, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import date, timedelta

# ──────────────── конфигурация ────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "replace_with_secure_key")

DATABASE = os.path.join(app.root_path, "app.db")

UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

# Open Food Facts (world‑mirror = стабильней)
OFF_BASE = "https://world.openfoodfacts.org"


# ──────────────── helpers ────────────────
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Создаём таблицы (если нет) и добавляем недостающие колонки."""
    db = get_db()
    cur = db.cursor()

    # Таблицы users, profiles, products, food_entries
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email     TEXT NOT NULL UNIQUE,
            password  TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id     INTEGER PRIMARY KEY,
            height_cm   REAL,
            weight_kg   REAL,
            age         INTEGER,
            gender      TEXT CHECK(gender IN ('male','female','other')),
            photo_path  TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id  INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            calories_per_100g REAL NOT NULL,
            protein_g REAL, fat_g REAL, carbs_g REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS food_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity_g REAL    NOT NULL,
            meal_no    INTEGER NOT NULL DEFAULT 1,
            entry_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id)    REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)
    # Добавляем meal_no, если ещё нет
    cols = [row[1] for row in db.execute("PRAGMA table_info(food_entries)")]
    if "meal_no" not in cols:
        db.execute("ALTER TABLE food_entries ADD COLUMN meal_no INTEGER DEFAULT 1")

    # Добавляем поля для конструктора рациона в profiles, если их нет
    cols = [row[1] for row in db.execute("PRAGMA table_info(profiles)")]
    extras = [
        ("diet_type", "TEXT"),
        ("activity_level", "TEXT"),
        ("bmr", "INTEGER"),
        ("tdee", "INTEGER"),
        ("target_cal", "INTEGER"),
        ("protein_g", "REAL"),
        ("fat_g", "REAL"),
        ("carbs_g", "REAL")
    ]
    for col, col_type in extras:
        if col not in cols:
            db.execute(f"ALTER TABLE profiles ADD COLUMN {col} {col_type}")

    db.commit()


with app.app_context():
    init_db()


# ──────────────── auth / регистрация ────────────────
@app.route('/')
def index():
    return render_template("index.html")


@app.route('/login', methods=['POST'])
def login():
    email = request.form.get('username')
    password = request.form.get('password')
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user and check_password_hash(user["password"], password):
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for('main'))
    return redirect(url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form['full_name']
        email = request.form['email']
        hashed_pw = generate_password_hash(request.form['password'])
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO users (full_name, email, password) VALUES (?, ?, ?)",
                (full_name, email, hashed_pw)
            )
            user_id = cur.lastrowid
            cur.execute("INSERT INTO profiles (user_id) VALUES (?)", (user_id,))
            db.commit()
        except sqlite3.IntegrityError:
            return redirect(url_for('register'))
        session.clear()
        session["user_id"] = user_id
        return redirect(url_for('main'))
    return render_template("register.html")


# ──────────────── главная страница (дашборд) ────────────────
@app.route('/main', methods=['GET', 'POST'])
def main():
    if "user_id" not in session:
        return redirect(url_for('index'))
    db = get_db()

    # обновляем профиль из сайдбара
    if request.method == 'POST':
        db.execute(
            """UPDATE profiles
               SET height_cm=?, weight_kg=?, age=?, gender=?
               WHERE user_id=?""",
            (
                request.form.get('height') or None,
                request.form.get('weight') or None,
                request.form.get('age')    or None,
                request.form.get('gender') or None,
                session["user_id"],
            ),
        )
        db.commit()
        # если у пользователя уже есть выбранная стратегия диеты — пересчитать и сохранить план
        prof = db.execute(
            "SELECT diet_type, activity_level, height_cm, weight_kg, age, gender "
            "FROM profiles WHERE user_id=?", (session["user_id"],)
        ).fetchone()
        if prof["diet_type"] and prof["activity_level"]:
            w = prof["weight_kg"] or 0
            h = prof["height_cm"] or 0
            a = prof["age"] or 0
            g = prof["gender"]
            # BMR (Mifflin–St Jeor)
            if g == 'male':
                bmr = 10 * w + 6.25 * h - 5 * a + 5
            elif g == 'female':
                bmr = 10 * w + 6.25 * h - 5 * a - 161
            else:
                bmr = 10 * w + 6.25 * h - 5 * a
            factors = {'low': 1.2, 'medium': 1.55, 'high': 1.725}
            tdee = bmr * factors.get(prof["activity_level"], 1.2)
            if prof["diet_type"] == 'mass':
                target_cal = tdee + 500
            elif prof["diet_type"] == 'cutting':
                target_cal = tdee - 500
            else:
                target_cal = tdee
            protein = (target_cal * 0.20) / 4
            fat     = (target_cal * 0.25) / 9
            carbs   = (target_cal * 0.55) / 4
            db.execute(
                "UPDATE profiles SET bmr=?, tdee=?, target_cal=?, protein_g=?, fat_g=?, carbs_g=? "
                "WHERE user_id=?",
                (
                    int(round(bmr)), int(round(tdee)), int(round(target_cal)),
                    int(round(protein)), int(round(fat)), int(round(carbs)),
                    session["user_id"]
                )
            )
            db.commit()

    # подгружаем профиль вместе с данными плана питания
    profile = db.execute(
        """
        SELECT height_cm, weight_kg, age, gender, photo_path,
               diet_type, activity_level, bmr, tdee,
               target_cal, protein_g, fat_g, carbs_g
        FROM profiles WHERE user_id=?
        """,
        (session["user_id"],)
    ).fetchone()

    # подсчёт сумм за сегодня
    rows = db.execute(
        """SELECT fe.quantity_g, p.calories_per_100g
           FROM food_entries fe
           JOIN products p ON p.id = fe.product_id
           WHERE fe.user_id = ?""",
        (session["user_id"],)
    ).fetchall()
    total_cal = 0
    for r in rows:
        total_cal += round((r["calories_per_100g"] or 0) * r["quantity_g"] / 100.0)
    norm_cal = profile["target_cal"] or 0

    return render_template("main.html", profile=profile, totals={"kcal": total_cal}, norm_cal=norm_cal)


# ──────────────── страница профиля ────────────────
@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if "user_id" not in session:
        return redirect(url_for('index'))
    db = get_db()

    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        db.execute("UPDATE users SET full_name=?, email=? WHERE id=?",
                   (full_name, email, session["user_id"]))
        new_pw = request.form.get('password')
        if new_pw:
            db.execute("UPDATE users SET password=? WHERE id=?",
                       (generate_password_hash(new_pw), session["user_id"]))
        photo = request.files.get('photo')
        if photo and allowed_file(photo.filename):
            filename = secure_filename(photo.filename)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            photo.save(save_path)
            db.execute("UPDATE profiles SET photo_path=? WHERE user_id=?",
                       (os.path.join("uploads", filename), session["user_id"]))
        db.commit()
        return redirect(url_for('profile'))

    profile = db.execute(
        """SELECT u.full_name, u.email, p.photo_path
           FROM users u JOIN profiles p ON u.id=p.user_id
           WHERE u.id=?""",
        (session["user_id"],)
    ).fetchone()
    return render_template("profile.html", profile=profile)


# ──────────────── база продуктов ────────────────
@app.route('/product-db', methods=['GET', 'POST'])
def product_db():
    if "user_id" not in session:
        return redirect(url_for('index'))
    next_page = request.args.get('next') or request.form.get('next') or 'main'

    products, query, error = [], None, None

    def parse_product(prod):
        nutr = prod.get("nutriments", {})
        return {
            "name":  prod.get("product_name_ru") or prod.get("product_name", ""),
            "per":   prod.get("nutrition_data_per", "100g"),
            "kcal":  nutr.get("energy-kcal_100g") or nutr.get("energy_100g"),
            "protein": nutr.get("proteins_100g"),
            "fat":     nutr.get("fat_100g"),
            "carbs":   nutr.get("carbohydrates_100g"),
        }

    if request.method == 'POST':
        query = request.form.get('query')
        params = {
            "search_terms": query, "search_simple": 1,
            "action": "process", "json": 1,
            "page_size": 10, "page": 1, "lc": "ru"
        }
        try:
            r = requests.get(f"{OFF_BASE}/cgi/search.pl", params=params, timeout=None)
            for prod in r.json().get("products", [])[:10]:
                products.append(parse_product(prod))
        except RequestException:
            error = "Сервер Open Food Facts недоступен, попробуйте позже."
    else:
        for _ in range(5):
            try:
                d = requests.get(f"{OFF_BASE}/api/v0/product/random.json",
                                 timeout=10).json()
                if d.get("status") == 1:
                    products.append(parse_product(d["product"]))
            except RequestException:
                continue

    return render_template("product_db.html",
                           products=products,
                           query=query,
                           error_message=error,
                           next=next_page)


@app.route('/product-db-data')
def product_db_data():
    if "user_id" not in session:
        return jsonify({'error': 'unauthorized'}), 401
    query = request.args.get('query', '')
    page = int(request.args.get('page', 1))
    params = {
        "search_terms": query, "search_simple": 1,
        "action": "process", "json": 1,
        "page_size": 10, "page": page, "lc": "ru"
    }
    try:
        r = requests.get(f"{OFF_BASE}/cgi/search.pl", params=params, timeout=10)
        data = [{
            "name": p.get("product_name_ru") or p.get("product_name", ""),
            "per": p.get("nutrition_data_per", "100g"),
            "kcal": (p.get("nutriments") or {}).get("energy-kcal_100g")
                    or (p.get("nutriments") or {}).get("energy_100g"),
            "protein": (p.get("nutriments") or {}).get("proteins_100g"),
            "fat":     (p.get("nutriments") or {}).get("fat_100g"),
            "carbs":   (p.get("nutriments") or {}).get("carbohydrates_100g")
        } for p in r.json().get("products", [])]
        return jsonify({'products': data})
    except RequestException:
        return jsonify({'error': 'Сервер Open Food Facts недоступен'}), 500


# ──────────────── добавление / удаление пищи ────────────────
@app.route('/add-food', methods=['POST'])
def add_food():
    if "user_id" not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    try:
        name = data['name']
        qty = float(data['qty'])
        meal_no = int(data.get('meal') or 1)
        kcal = float(data.get('kcal') or 0)
        protein = float(data.get('protein') or 0)
        fat = float(data.get('fat') or 0)
        carbs = float(data.get('carbs') or 0)
        if meal_no not in (1, 2, 3):
            meal_no = 1
    except (KeyError, ValueError):
        return jsonify({'error': 'invalid data'}), 400

    db = get_db()
    prod = db.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
    if prod:
        prod_id = prod["id"]
    else:
        db.execute(
            "INSERT INTO products(name, calories_per_100g, protein_g, fat_g, carbs_g) VALUES(?,?,?,?,?)",
            (name, kcal, protein, fat, carbs)
        )
        prod_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO food_entries(user_id, product_id, quantity_g, meal_no) VALUES(?,?,?,?)",
        (session["user_id"], prod_id, qty, meal_no)
    )
    db.commit()
    return jsonify({'redirect': url_for('food_entry'), 'meal': meal_no}), 200


@app.route('/delete-food', methods=['POST'])
def delete_food():
    if "user_id" not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    try:
        entry_id = int(data['id'])
    except (KeyError, ValueError):
        return jsonify({'error': 'invalid id'}), 400
    db = get_db()
    db.execute("DELETE FROM food_entries WHERE id=? AND user_id=?",
               (entry_id, session["user_id"]))
    db.commit()
    return jsonify({'ok': True})


# ──────────────── страница «Приём пищи» ────────────────
@app.route('/food-entry')
def food_entry():
    if "user_id" not in session:
        return redirect(url_for('index'))
    db = get_db()
    # determine start of week (Monday)
    week_start_str = request.args.get('week_start')
    if week_start_str:
        try:
            week_start = date.fromisoformat(week_start_str)
        except ValueError:
            week_start = date.today() - timedelta(days=date.today().weekday())
    else:
        week_start = date.today() - timedelta(days=date.today().weekday())
    # build list of dates for week
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    # fetch history: total calories per day
    history_data = []
    for d in week_dates:
        row = db.execute(
            """SELECT SUM((p.calories_per_100g * fe.quantity_g) / 100.0) AS total
               FROM food_entries fe
               JOIN products p ON p.id = fe.product_id
               WHERE fe.user_id = ? AND date(fe.entry_time) = ?""",
            (session["user_id"], d.isoformat())
        ).fetchone()
        total = row["total"] or 0
        history_data.append({"date": d.isoformat(), "calories": int(round(total))})

    meals = {1: [], 2: [], 3: []}
    rows = db.execute(
        """SELECT fe.id AS entry_id, fe.meal_no, fe.quantity_g,
                  p.name, p.calories_per_100g,
                  p.protein_g, p.fat_g, p.carbs_g
           FROM food_entries fe
           JOIN products p ON p.id=fe.product_id
           WHERE fe.user_id=?
           ORDER BY fe.id DESC""",
        (session["user_id"],)
    ).fetchall()
    for r in rows:
        factor = r["quantity_g"] / 100.0
        meals[r["meal_no"]].append({
            "id":   r["entry_id"],
            "name": r["name"],
            "qty":  int(r["quantity_g"]),
            "kcal":    round((r["calories_per_100g"] or 0) * factor),
            "protein": round((r["protein_g"]         or 0) * factor, 1),
            "fat":     round((r["fat_g"]             or 0) * factor, 1),
            "carbs":   round((r["carbs_g"]           or 0) * factor, 1),
        })
    # мини‑сводки по приёмам
    meal_sums = {i: {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0} for i in (1,2,3)}
    for m_no, items in meals.items():
        for r in items:
            meal_sums[m_no]["kcal"]    += r["kcal"]
            meal_sums[m_no]["protein"] += r["protein"]
            meal_sums[m_no]["fat"]     += r["fat"]
            meal_sums[m_no]["carbs"]   += r["carbs"]
    for s in meal_sums.values():
        s["kcal"] = int(s["kcal"])
        for k in ("protein","fat","carbs"):
            s[k] = round(s[k], 1)
    # суммарно за день
    totals = {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0}
    for s in meal_sums.values():
        totals["kcal"]    += s["kcal"]
        totals["protein"] += s["protein"]
        totals["fat"]     += s["fat"]
        totals["carbs"]   += s["carbs"]

    # Получаем норму калорий из профиля
    profile = db.execute(
        "SELECT target_cal FROM profiles WHERE user_id = ?",
        (session["user_id"],)
    ).fetchone()
    norm_cal = profile["target_cal"] or 0

    return render_template(
        "food_entry.html",
        meals=meals,
        meal_sums=meal_sums,
        totals=totals,
        norm_cal=norm_cal,
        history_data=history_data,
        week_start=week_start.isoformat()
    )


# ──────────────── конструктор рациона ────────────────
@app.route('/diet-constructor', methods=['GET', 'POST'])
def diet_constructor():
    if "user_id" not in session:
        return redirect(url_for('index'))
    db = get_db()
    profile = db.execute(
        """SELECT height_cm, weight_kg, age, gender,
                  diet_type, activity_level, bmr, tdee,
                  target_cal, protein_g, fat_g, carbs_g
           FROM profiles WHERE user_id=?""",
        (session["user_id"],)
    ).fetchone()

    plan = None
    if request.method == 'POST':
        diet_type = request.form['diet_type']
        activity_level = request.form['activity_level']

        weight = profile['weight_kg'] or 0
        height = profile['height_cm'] or 0
        age    = profile['age'] or 0
        gender = profile['gender']

        # BMR (Mifflin–St Jeor)
        if gender == 'male':
            bmr = 10 * weight + 6.25 * height - 5 * age + 5
        elif gender == 'female':
            bmr = 10 * weight + 6.25 * height - 5 * age - 161
        else:
            bmr = 10 * weight + 6.25 * height - 5 * age

        factors = {'low':1.2, 'medium':1.55, 'high':1.725}
        factor = factors.get(activity_level, 1.2)
        tdee = bmr * factor

        if diet_type == 'mass':
            target_cal = tdee + 500
        elif diet_type == 'cutting':
            target_cal = tdee - 500
        else:
            target_cal = tdee

        protein_g = (target_cal * 0.20) / 4
        fat_g     = (target_cal * 0.25) / 9
        carbs_g   = (target_cal * 0.55) / 4

        plan = {
            'bmr':       int(round(bmr)),
            'tdee':      int(round(tdee)),
            'target_cal':int(round(target_cal)),
            'protein_g': int(round(protein_g)),
            'fat_g':     int(round(fat_g)),
            'carbs_g':   int(round(carbs_g)),
        }

        # Сохраняем в БД
        db.execute("""
            UPDATE profiles
            SET diet_type=?, activity_level=?, bmr=?, tdee=?,
                target_cal=?, protein_g=?, fat_g=?, carbs_g=?
            WHERE user_id=?
        """, (
            diet_type, activity_level, plan['bmr'], plan['tdee'],
            plan['target_cal'], plan['protein_g'], plan['fat_g'], plan['carbs_g'],
            session["user_id"]
        ))
        db.commit()
        return redirect(url_for('diet_constructor'))

    # Если уже рассчитан
    if profile['target_cal'] is not None:
        plan = {
            'bmr':       profile['bmr'],
            'tdee':      profile['tdee'],
            'target_cal':profile['target_cal'],
            'protein_g': profile['protein_g'],
            'fat_g':     profile['fat_g'],
            'carbs_g':   profile['carbs_g'],
        }

    return render_template("diet_constructor.html",
                           profile=profile,
                           plan=plan)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True)