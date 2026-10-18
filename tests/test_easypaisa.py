
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from tests.test_app import client_for, csrf, register_and_verify


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + b"\x00" * 13
)


def create_easypaisa_order(client):
    page = client.get("/enroll?plan=pro")
    assert page.status_code == 200

    response = client.post(
        "/enroll/easypaisa/order",
        data={
            "csrf_token": csrf(page.text),
            "plan": "pro",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    order_id = parse_qs(
        urlparse(response.headers["location"]).query
    )["order_id"][0]
    return int(order_id)


def submit_proof(client, order_id, transaction_id="EP12345678"):
    page = client.get(f"/enroll/return?order_id={order_id}")
    assert page.status_code == 200

    return client.post(
        f"/payments/easypaisa/{order_id}",
        data={
            "csrf_token": csrf(page.text),
            "transaction_id": transaction_id,
        },
        files={
            "proof": ("proof.png", PNG_BYTES, "image/png"),
        },
        follow_redirects=False,
    )


def test_easypaisa_order_and_proof_submission(tmp_path: Path):
    client = client_for(
        tmp_path,
        easypaisa_receiving_number="03000000000",
    )
    register_and_verify(client, tmp_path)

    order_id = create_easypaisa_order(client)
    response = submit_proof(client, order_id)

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/enroll/return?order_id={order_id}"
    )

    submission = client.app.state.db.easypaisa_for_user_order(
        order_id,
        client.app.state.db.get_user_by_canonical(
            "learnerone@gmail.com"
        )["id"],
    )
    assert submission is not None
    assert submission["transaction_id"] == "EP12345678"
    assert submission["status"] == "pending"

    proof_path = tmp_path / "payment_proofs" / submission["proof_filename"]
    assert proof_path.is_file()
    assert proof_path.read_bytes() == PNG_BYTES


def test_easypaisa_duplicate_transaction_is_rejected(tmp_path: Path):
    client = client_for(
        tmp_path,
        easypaisa_receiving_number="03000000000",
    )
    register_and_verify(client, tmp_path)

    first_order = create_easypaisa_order(client)
    assert submit_proof(client, first_order, "EP12345678").status_code == 303

    second_order = create_easypaisa_order(client)
    assert submit_proof(client, second_order, "EP12345678").status_code == 303

    submissions = client.app.state.db.easypaisa_submissions()
    assert len(submissions) == 1
    assert submissions[0]["order_id"] == first_order


def test_easypaisa_approval_activates_plan(tmp_path: Path):
    client = client_for(
        tmp_path,
        easypaisa_receiving_number="03000000000",
    )
    register_and_verify(client, tmp_path)

    student = client.app.state.db.get_user_by_canonical(
        "learnerone@gmail.com"
    )
    order_id = create_easypaisa_order(client)
    assert submit_proof(client, order_id).status_code == 303

    submission = client.app.state.db.easypaisa_for_user_order(
        order_id,
        student["id"],
    )
    assert submission is not None

    account_page = client.get("/account")
    logout = client.post(
        "/logout",
        data={"csrf_token": csrf(account_page.text)},
        follow_redirects=False,
    )
    assert logout.status_code == 303

    register_and_verify(
        client,
        tmp_path,
        email="admin.test@gmail.com",
        name="Ada Admin",
    )

    page = client.get("/admin/easypaisa")
    assert page.status_code == 200
    assert "EP12345678" in page.text

    reviewed = client.post(
        f"/admin/easypaisa/{submission['id']}/review",
        data={
            "csrf_token": csrf(page.text),
            "action": "approve",
            "review_note": "Test payment approved.",
        },
        follow_redirects=False,
    )
    assert reviewed.status_code == 303

    assert client.app.state.db.get_order(order_id)["status"] == "paid"
    updated_student = client.app.state.db.get_user_by_canonical(
        "learnerone@gmail.com"
    )
    assert updated_student["plan"] == "pro"

    updated_submission = client.app.state.db.easypaisa_for_user_order(
        order_id,
        student["id"],
    )
    assert updated_submission["status"] == "approved"

    repeated_page = client.get("/admin/easypaisa")
    repeated = client.post(
        f"/admin/easypaisa/{submission['id']}/review",
        data={
            "csrf_token": csrf(repeated_page.text),
            "action": "approve",
            "review_note": "Repeated approval attempt.",
        },
        follow_redirects=False,
    )
    assert repeated.status_code == 303
    assert client.app.state.db.get_order(order_id)["status"] == "paid"


def test_easypaisa_requires_valid_csrf_and_image(tmp_path: Path):
    client = client_for(
        tmp_path,
        easypaisa_receiving_number="03000000000",
    )
    register_and_verify(client, tmp_path)

    order_id = create_easypaisa_order(client)

    no_csrf = client.post(
        f"/payments/easypaisa/{order_id}",
        data={"transaction_id": "EP12345678"},
        files={"proof": ("proof.png", PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert no_csrf.status_code == 303

    page = client.get(f"/enroll/return?order_id={order_id}")
    invalid_image = client.post(
        f"/payments/easypaisa/{order_id}",
        data={
            "csrf_token": csrf(page.text),
            "transaction_id": "EP87654321",
        },
        files={"proof": ("fake.png", b"not an image", "image/png")},
        follow_redirects=False,
    )
    assert invalid_image.status_code == 303

    user = client.app.state.db.get_user_by_canonical(
        "learnerone@gmail.com"
    )
    assert client.app.state.db.easypaisa_for_user_order(
        order_id, user["id"]
    ) is None
