import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BatchExtractModal from "../components/BatchExtractModal";
import * as api from "../services/api";

vi.mock("../services/api");

beforeEach(() => {
  api.startBatchExtract.mockReset();
  api.getBatchStatus.mockReset();
  api.cancelBatch.mockReset();
});

const docs = [
  { id: "abc", filename: "a.pdf" },
  { id: "def", filename: "b.pdf" },
];

test("Start kicks off the batch and polls until status flips to done", async () => {
  const user = userEvent.setup();
  api.startBatchExtract.mockResolvedValue({
    job_id: "job-1",
    total: 2,
    status: "running",
  });
  // First poll returns half-done, second returns done.
  api.getBatchStatus
    .mockResolvedValueOnce({
      status: "running",
      completed: 1,
      total: 2,
      results: { abc: { fields: [] } },
      errors: {},
    })
    .mockResolvedValueOnce({
      status: "done",
      completed: 2,
      total: 2,
      results: { abc: { fields: [] }, def: { fields: [] } },
      errors: {},
    });

  render(
    <BatchExtractModal
      open
      documents={docs}
      definitionId="invoice"
      definitionLabel="Invoice"
      onClose={() => {}}
    />,
  );

  await user.click(screen.getByTestId("batch-start"));
  await waitFor(() =>
    expect(api.startBatchExtract).toHaveBeenCalledWith(["abc", "def"], "invoice"),
  );
  // Eventually we transition to "done" and Download/Close show up.
  expect(await screen.findByTestId("batch-download")).toBeInTheDocument();
});

test("Cancel-run button calls cancelBatch and waits for terminal status", async () => {
  const user = userEvent.setup();
  api.startBatchExtract.mockResolvedValue({
    job_id: "job-2",
    total: 2,
    status: "running",
  });
  // First poll: still running. Second poll (after cancel): cancelled.
  api.getBatchStatus
    .mockResolvedValueOnce({
      status: "running",
      completed: 0,
      total: 2,
      results: {},
      errors: {},
    })
    .mockResolvedValueOnce({
      status: "cancelled",
      completed: 1,
      total: 2,
      results: {},
      errors: {},
    });
  api.cancelBatch.mockResolvedValue({ cancelling: true });

  render(
    <BatchExtractModal
      open
      documents={docs}
      definitionId="invoice"
      definitionLabel="Invoice"
      onClose={() => {}}
    />,
  );

  await user.click(screen.getByTestId("batch-start"));
  const cancel = await screen.findByTestId("batch-cancel");
  await user.click(cancel);
  await waitFor(() => expect(api.cancelBatch).toHaveBeenCalledWith("job-2"));
});

test("surfaces a start error inline without entering the running state", async () => {
  const user = userEvent.setup();
  api.startBatchExtract.mockRejectedValue(new Error("definition not found"));

  render(
    <BatchExtractModal
      open
      documents={docs}
      definitionId="invoice"
      definitionLabel="Invoice"
      onClose={() => {}}
    />,
  );

  await user.click(screen.getByTestId("batch-start"));
  expect(await screen.findByText(/definition not found/)).toBeInTheDocument();
  // The Start button is still visible (state went back to idle).
  expect(screen.getByTestId("batch-start")).toBeInTheDocument();
});
