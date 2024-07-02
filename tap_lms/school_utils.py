import frappe
import random
import string

def generate_unique_keyword(name1):
    words = name1.split()
    first_two_letters = "".join([word[:2].upper() for word in words if word])
    random_number = random.randint(10, 99)
    random_letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    return first_two_letters + str(random_number) + random_letters

