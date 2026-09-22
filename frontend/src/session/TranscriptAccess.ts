import { createContext, useContext } from 'react';
import {
  getHistoryBlocks, getHistoryBlockDetails, generateProcessSummary,
  getSharedHistoryBlocks, getSharedHistoryBlockDetails,
} from '../api';

/** Authorization changes the transport, not transcript paging or rendering. */
export interface TranscriptAccess {
  readHistory: typeof getHistoryBlocks;
  readDetails: typeof getHistoryBlockDetails;
  generateSummary?: typeof generateProcessSummary;
}

const ownerAccess: TranscriptAccess = {
  readHistory: getHistoryBlocks,
  readDetails: getHistoryBlockDetails,
  generateSummary: generateProcessSummary,
};

export const TranscriptAccessContext = createContext<TranscriptAccess>(ownerAccess);
export const useTranscriptAccess = () => useContext(TranscriptAccessContext);

export function sharedTranscriptAccess(token: string): TranscriptAccess {
  return {
    readHistory: (_sessionId, before, limit, init) => getSharedHistoryBlocks(token, before, limit, init),
    readDetails: (_sessionId, blockId, cursor, init) => getSharedHistoryBlockDetails(token, blockId, cursor, init),
  };
}
