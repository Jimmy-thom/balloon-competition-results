# Deployment — v8

## Recommended free beta architecture

- **Web app:** Render Free Web Service
- **Database:** Supabase Free Postgres
- **Source:** GitHub repository
- **HTTPS:** Render managed TLS

Render's free filesystem is ephemeral, so do not use local SQLite for production data. The app now switches to Postgres whenever `DATABASE_URL` is present and retains SQLite for local development.

Supabase's current Free plan includes a 500 MB Postgres database, but free projects pause after one week of inactivity. This is suitable for the initial beta; upgrade before treating the service as mission-critical.

## Render settings

Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn app:app --workers 1 --threads 4 --timeout 120`

Health check:
`/healthz`

Environment variable:
`DATABASE_URL=<Supabase Postgres connection string>`

## First deployment

1. Create a GitHub repository and push this folder.
2. Create a Supabase Free project and copy its Postgres connection string.
3. Create a Render Web Service from the GitHub repository.
4. Set the build/start commands above.
5. Add `DATABASE_URL` in Render Environment Variables.
6. Deploy and open `/healthz`.
7. Run the importer once with the same `DATABASE_URL` to seed the production database.
8. Only after the beta is verified, attach a custom domain.
