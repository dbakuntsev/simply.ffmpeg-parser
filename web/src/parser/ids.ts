let tokenCounter = 0;
let bindingCounter = 0;
let issueCounter = 0;

export function resetIds() {
  tokenCounter = 0;
  bindingCounter = 0;
  issueCounter = 0;
}

export function nextTokenId() {
  tokenCounter += 1;
  return `tok_${tokenCounter}`;
}

export function nextBindingId() {
  bindingCounter += 1;
  return `bind_${bindingCounter}`;
}

export function nextIssueId() {
  issueCounter += 1;
  return `issue_${issueCounter}`;
}
