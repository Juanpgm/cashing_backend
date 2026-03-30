"""Quick integration test against running server."""
import json
import httpx

base = "http://127.0.0.1:8000/api/v1"

# 1. Register
print("=== REGISTER ===")
r = httpx.post(f"{base}/auth/register", json={
    "email": "juan@test.com",
    "password": "MiPassword123!",
    "nombre": "Juan Perez",
    "cedula": "1234567890",
    "telefono": "+573001234567",
})
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 2. Login
print("\n=== LOGIN ===")
r = httpx.post(f"{base}/auth/login", json={
    "email": "juan@test.com",
    "password": "MiPassword123!",
})
print(f"Status: {r.status_code}")
tokens = r.json()
print(json.dumps(tokens, indent=2))

access = tokens["access_token"]
refresh = tokens["refresh_token"]

# 3. Get Me
print("\n=== GET ME ===")
r = httpx.get(f"{base}/auth/me", headers={"Authorization": f"Bearer {access}"})
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 4. Update Me
print("\n=== UPDATE ME ===")
r = httpx.put(
    f"{base}/auth/me",
    headers={"Authorization": f"Bearer {access}"},
    json={"nombre": "Juan Carlos Perez"},
)
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 5. Refresh Token
print("\n=== REFRESH TOKEN ===")
r = httpx.post(f"{base}/auth/refresh", json={"refresh_token": refresh})
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 6. Duplicate Register (should fail 409)
print("\n=== DUPLICATE REGISTER (expect 409) ===")
r = httpx.post(f"{base}/auth/register", json={
    "email": "juan@test.com",
    "password": "OtherPass1!",
    "nombre": "Otro Juan",
    "cedula": "9999999999",
    "telefono": "+573009999999",
})
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 7. Wrong password (should fail 401)
print("\n=== WRONG PASSWORD (expect 401) ===")
r = httpx.post(f"{base}/auth/login", json={
    "email": "juan@test.com",
    "password": "WrongPassword!",
})
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 8. Unauthorized /me (should fail 401)
print("\n=== UNAUTHORIZED ME (expect 401) ===")
r = httpx.get(f"{base}/auth/me")
print(f"Status: {r.status_code}")
print(json.dumps(r.json(), indent=2))

# 9. Docs page
print("\n=== DOCS ===")
r = httpx.get("http://127.0.0.1:8000/docs")
print(f"Status: {r.status_code} (Swagger UI available)")

print("\n✅ All integration checks completed!")
