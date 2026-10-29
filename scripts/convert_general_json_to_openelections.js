const fs = require('fs');
const path = require('path');

const allElections = [
  { year: 2012, date: '20121106' },
  { year: 2014, date: '20141104' },
  { year: 2022, date: '20221108' },
  { year: 2024, date: '20241105' },
];
const requestedYears = new Set(process.argv.slice(2).map(Number));
const elections = requestedYears.size
  ? allElections.filter(({ year }) => requestedYears.has(year))
  : allElections;

const partyMap = new Map([
  ['democratic party', 'DEM'],
  ['republican party', 'REP'],
  ['libertarian party', 'LIB'],
  ['green party', 'GRN'],
  ['nevada green party', 'GRN'],
  ['independent', 'IND'],
  ['nonpartisan', 'NOP'],
  ['non-partisan', 'NOP'],
]);

const partyOverrides = new Map([
  ['2012|President|Gary Johnson / James P. Gray', 'LIB'],
  ['2012|President|Peta Lindsay / Yari Osorio', 'SOC'],
  ['2012|U.S. House|Jessica Paxton', 'LIB'],
  ['2012|U.S. House|Chris Hayes', 'LIB'],
  ['2012|U.S. House|David Pangrac', 'LIB'],
  ['2012|U.S. House|Bobby Tullis', 'LIB'],
  ['2012|State Senate|Frank Gilbert', 'LIB'],
  ['2012|State Senate|David E. Dinwiddie', 'LIB'],
  ['2012|State Senate|Glen Schwarz', 'LIB'],
  ['2012|State Representative|Debrah Standiford', 'LIB'],
  ['2012|State Representative|Rodger Paxton', 'LIB'],
  ['2014|U.S. Senate|Nathan LaFrance', 'LIB'],
  ['2014|U.S. House|Brian Scott Willhite', 'LIB'],
  ['2014|U.S. House|Debbie Standiford', 'LIB'],
  ['2014|U.S. House|Grant Brand', 'LIB'],
  ['2014|U.S. House|Ken Hamilton', 'LIB'],
  ['2014|Governor|Frank Gilbert', 'LIB'],
  ['2014|Lieutenant Governor|Christopher Olson', 'LIB'],
  ['2014|Attorney General|Aaron Cash', 'LIB'],
  ['2014|Secretary of State|Jacob D. Holloway', 'LIB'],
  ['2014|State Treasurer|Chris Hayes', 'LIB'],
  ['2014|Auditor of State|Brian Leach', 'LIB'],
  ['2014|Commissioner of State Lands|Elvis D. Presley', 'LIB'],
  ['2014|State Representative|Wayne Willems', 'LIB'],
  ['2014|State Representative|Marc Rosson', 'LIB'],
  ['2014|State Representative|Greg Deckelman', 'LIB'],
  ['2014|State Representative|Rodger Paxton', 'LIB'],
  ['2014|State Representative|Taylor Watkins', 'LIB'],
  ['2014|State Representative|Eddie Moser', 'LIB'],
  ['2014|State Representative|Michael J. Kalagias', 'LIB'],
  ['2022|U.S. Senate|Kenneth Cates', 'LIB'],
  ['2022|Governor|Ricky Dale Harrington, Jr.', 'LIB'],
  ['2022|Auditor of State|Simeon Snow', 'LIB'],
  ['2022|Lieutenant Governor|Frank Gilbert', 'LIB'],
  ['2022|U.S. House|Michael White', 'LIB'],
  ['2022|U.S. House|Michael J. Kalagias', 'LIB'],
  ['2022|U.S. House|Gregory Maxwell', 'LIB'],
  ['2024|President|Chase Oliver/Mike ter Maat', 'LIB'],
  ['2024|President|Robert F. Kennedy, Jr./Nicole Shanahan', 'IND'],
  ['2024|President|Peter Sonski/Lauren Onak', 'ASP'],
  ['2024|U.S. House|Steve G. Parsons', 'LIB'],
  ['2024|U.S. House|Bobby Wilson', 'LIB'],
  ['2024|State Treasurer|Michael Pakko', 'LIB'],
]);

function normalizeCounty(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeParty(rawParty, rawCandidate, office) {
  const partyText = String(rawParty || '').trim();
  const mapped = partyMap.get(partyText.toLowerCase());
  let candidate = String(rawCandidate || '').trim();

  if (mapped) return { party: mapped, candidate };

  const prefix = candidate.match(/^(DEM|REP|LIB|IND|GRN|NOP|NPA)\s+(.+)$/i);
  if (prefix) {
    return {
      party: prefix[1].toUpperCase() === 'NPA' ? 'IND' : prefix[1].toUpperCase(),
      candidate: prefix[2].trim(),
    };
  }

  if (/^(?:Issue\b|Local (?:Issue|Measure)\b|.*\b(?:Question|Amendment|Referendum|Ordinance|Annexation|Bonds?|Tax)\b)/i.test(office)) {
    return { party: 'NOP', candidate };
  }

  if (/\b(?:Supreme Court|Court of Appeals|Circuit Judge|District Judge|Judge)\b/i.test(office)) {
    return { party: 'NOP', candidate };
  }

  return { party: '', candidate };
}

function normalizeOffice(rawOffice) {
  const source = String(rawOffice || '').trim();
  let match;

  if (/^U\.S\. President(?: and Vice President)?$/i.test(source)) {
    return { office: 'President', district: '' };
  }
  if (/^U\.S\. Senate$/i.test(source)) {
    return { office: 'U.S. Senate', district: '' };
  }
  match = source.match(/^U\.S\. Congress District\s+0*(\d+)$/i);
  if (match) {
    return { office: 'U.S. House', district: String(Number(match[1])) };
  }
  match = source.match(/^State Senate District\s+0*(\d+)$/i);
  if (match) {
    return { office: 'State Senate', district: String(Number(match[1])) };
  }
  match = source.match(/^State Representative District\s+0*(\d+)$/i);
  if (match) {
    return { office: 'State Representative', district: String(Number(match[1])) };
  }
  match = source.match(/^Justice of (?:the )?Peace[,]?\s+District\s*#?\s*0*(\d+)(?:\s+District\s+0*\d+)?$/i);
  if (match) {
    return { office: 'Justice of the Peace', district: String(Number(match[1])) };
  }

  return { office: source, district: '' };
}

function csvCell(value) {
  const text = String(value ?? '');
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function rowKey(row) {
  return [row.county, row.precinct, row.office, row.district, row.party, row.candidate, row.votes].join('\u001f');
}

function convertElection({ year, date }) {
  const inputPath = path.join('data', `${year}_General.json`);
  const outputPath = path.join('data', `${date}__ar__general__precinct.csv`);
  const payload = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
  const rows = [];
  const errors = [];
  const exactKeys = new Map();
  const aggregateMismatchDetails = [];

  for (const contest of payload.ContestData || []) {
    const normalizedContest = normalizeOffice(contest.ContestName);
    const precinctCandidateTotals = new Map();
    const canonicalCandidates = new Map(
      (contest.Candidates || []).map((candidate) => [
        String(candidate.Name || '').trim().toLowerCase(),
        String(candidate.Name || '').trim(),
      ]),
    );

    for (const county of contest.Counties || []) {
      const countyName = normalizeCounty(county.CountyName);
      for (const precinct of county.Precincts || []) {
        const precinctName = String(precinct.PrecinctName || '').trim();
        for (const candidateEntry of precinct.Candidates || []) {
          const rawCandidate = String(candidateEntry.Name || '').trim();
          const canonicalCandidate = canonicalCandidates.get(rawCandidate.toLowerCase()) || rawCandidate;
          const normalizedCandidate = normalizeParty(
            candidateEntry.PartyName,
            canonicalCandidate,
            normalizedContest.office,
          );
          if (!normalizedCandidate.party) {
            normalizedCandidate.party = partyOverrides.get(
              `${year}|${normalizedContest.office}|${normalizedCandidate.candidate}`,
            ) || '';
          }
          const votes = Number(candidateEntry.TotalVotes);
          const row = {
            county: countyName,
            precinct: precinctName,
            office: normalizedContest.office,
            district: normalizedContest.district,
            party: normalizedCandidate.party,
            candidate: normalizedCandidate.candidate,
            votes,
          };

          if (!row.county || !row.precinct || !row.office || !row.candidate) {
            errors.push(`Missing required value: ${JSON.stringify(row)}`);
          }
          if (!Number.isInteger(votes) || votes < 0) {
            errors.push(`Invalid vote total: ${JSON.stringify(row)}`);
          }

          rows.push(row);
          const key = rowKey(row);
          exactKeys.set(key, (exactKeys.get(key) || 0) + 1);
          const candidateKey = rawCandidate.toLowerCase();
          precinctCandidateTotals.set(
            candidateKey,
            (precinctCandidateTotals.get(candidateKey) || 0) + votes,
          );
        }
      }
    }

    for (const candidate of contest.Candidates || []) {
      const expected = Number(candidate.TotalVotes);
      const actual = precinctCandidateTotals.get(String(candidate.Name || '').trim().toLowerCase()) || 0;
      if (expected !== actual) {
        aggregateMismatchDetails.push({
          contest: contest.ContestName,
          candidate: candidate.Name,
          expected,
          actual,
          difference: actual - expected,
        });
      }
    }
  }

  const headers = ['county', 'precinct', 'office', 'district', 'party', 'candidate', 'votes'];
  const csv = [
    headers.join(','),
    ...rows.map((row) => headers.map((header) => csvCell(row[header])).join(',')),
  ].join('\r\n') + '\r\n';
  fs.writeFileSync(outputPath, csv, 'utf8');

  const exactDuplicateGroups = [...exactKeys.values()].filter((count) => count > 1).length;
  const counties = new Set(rows.map((row) => row.county));
  const precincts = new Set(rows.map((row) => `${row.county}\u001f${row.precinct}`));
  const contests = new Set(rows.map((row) => `${row.office}\u001f${row.district}`));
  const voteTotal = rows.reduce((sum, row) => sum + row.votes, 0);

  if (errors.length) {
    throw new Error(`${year}: ${errors.slice(0, 10).join('\n')}`);
  }

  return {
    year,
    inputPath,
    outputPath,
    rows: rows.length,
    counties: counties.size,
    precincts: precincts.size,
    contests: contests.size,
    voteTotal,
    exactDuplicateGroups,
    aggregateMismatches: aggregateMismatchDetails.length,
    aggregateMismatchDetails,
  };
}

const summaries = elections.map(convertElection);
for (const summary of summaries) {
  console.log(JSON.stringify(summary));
}
