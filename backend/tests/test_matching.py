from datetime import date

from app import matching as m


def test_mohd_arif_k_matches_mohammed_arif_khan():
    r = m.match("Mohd Arif K.", date(2006, 4, 7), "Mohammed Arif Khan", date(2006, 4, 7))
    assert r["passed"] and r["name_level"] == "AGREE"


def test_day_month_swap_detected_with_fix():
    r = m.match("Mohd Arif K.", date(2006, 7, 4), "Mohammed Arif Khan", date(2006, 4, 7))
    assert not r["passed"] and r["dob_level"] == "SWAPPED" and r["probable"]
    assert r["fixes"][0]["field"] == "dob" and r["fixes"][0]["suggested"] == "2006-04-07"


def test_transliteration_variants_agree():
    for a, b in [("Sarma", "Sharma"), ("Vijay Kumar", "Wijay Kr"), ("Mohammad Imran", "Md Imran"), ("Smt. Lakshmi Iyer", "Laxmi Iyer")]:
        assert m.name_score(a, b) >= 0.85, (a, b, m.name_score(a, b))


def test_surname_first_order_is_tolerated():
    assert m.name_score("Khan Mohammed Arif", "Mohammed Arif Khan") >= 0.95


def test_different_people_do_not_match():
    r = m.match("Rahul Verma", date(2005, 1, 1), "Priya Nair", date(1999, 9, 9))
    assert not r["passed"] and not r["probable"] and not r["fixes"]


def test_dob_parsing_formats():
    assert m.parse_dob("04-07-2006") == date(2006, 7, 4)
    assert m.parse_dob("2006-07-04") == date(2006, 7, 4)
    assert m.parse_dob("31-02-2006") is None and m.parse_dob("garbage") is None


def test_weights_are_explainable_bits():
    r = m.match("A B", date(2000, 1, 1), "A B", date(2000, 1, 1))
    assert r["weights_bits"]["name"] > 0 and r["weights_bits"]["dob"] > 0 and r["prob"] > 0.999
