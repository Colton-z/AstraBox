# Runtime preparation fixture

Install this package in the candidate server image. Its entry points provide
the preparation and credential-binding fixtures used by the existing
`deployment_shape` cases. These providers belong in an isolated test
deployment; ordinary deployments do not install them.

## Streaming and model idle time

`runtime_preparation_test.py::test_a_delivered_message_is_prepared_once_and_streams_while_it_is_answered`
uses the preparation provider and the `model_header_gate` module. The gate
forwards real model requests and holds one response's headers for 65 seconds.
It returns the upstream response body unchanged. The case verifies one attempt,
the actual nginx peer, the native reply, its live stream and completed history,
along with the existing preparation and streaming latency assertions.

Configure the isolated candidate with an external inference gateway so its
model port 80 is free. Run `python -m uvicorn model_header_gate:app --host
0.0.0.0 --port 80` in the candidate server's network namespace, sharing its
private `/data` volume. The candidate's model base points through the shipped
sandbox edge to this listener. The gate's upstream points to the actual
inference gateway. Keep the model credential on that existing request path.

Write `/data/runtime-preparation-model-gate-config.json` before starting the
listener, with these fields:

```json
{
  "upstream": "http://inference-gateway:80",
  "edge_container": "the-actual-candidate-sandbox-edge-container-id"
}
```

The upstream must resolve from the server's network namespace. The edge handle
must identify the container that native requests traverse; the case compares
its network addresses with the peer recorded by the gate. Keep the shipped
edge configuration intact.

The case arms and disarms the gate through its private control file and reads
its request journal from the same volume. Deployment configuration may read
the authenticated model catalogue; it does not send a completion. Run only
the selected case with its existing 180-second budget. Preserve the journal,
native custody, test result and original-deployment baselines before retiring
the task-owned resources and model credential.
