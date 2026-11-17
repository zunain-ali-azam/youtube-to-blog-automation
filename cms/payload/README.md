# Payload config for yt2blog

yt2blog writes posts shaped like the `posts` collection in Payload's official website template
([templates/website](https://github.com/payloadcms/payload/tree/main/templates/website)). The template
doesn't have everything a generated post contains, so add these to your project. The paths below are the
template's.

## 1. The YouTube block

Copy the [`YouTube/`](YouTube) folder to `src/blocks/YouTube/`. In `src/collections/Posts/index.ts`, add it
to the `content` editor's blocks:

```ts
import {YouTube} from '../../blocks/YouTube/config'

// in the content field's lexicalEditor features
BlocksFeature({blocks: [Banner, Code, MediaBlock, YouTube]}),
```

## 2. More code languages

The template's Code block (`src/blocks/Code/config.ts`) only offers TypeScript, JavaScript and CSS. yt2blog
sends the language the video uses, such as `python`, `bash` or `html`, and a draft with another language
can't be published until it's in the list. Widen the `language` options:

```ts
options: ['typescript', 'javascript', 'tsx', 'jsx', 'html', 'css', 'json', 'bash', 'python', 'go',
  'rust', 'java', 'csharp', 'php', 'ruby', 'sql', 'yaml', 'plaintext'].map((value) => ({label: value, value})),
```

## 3. The extra post fields

Copy [`postFields.ts`](postFields.ts) into `src/collections/Posts/` and spread it into the collection's fields:

```ts
import {yt2blogPostFields} from './postFields'

// in the Posts collection
fields: [
  // ...the template's fields
  ...yt2blogPostFields,
],
```

Then run `pnpm payload generate:types`, and `pnpm payload migrate:create` if you use Postgres.

## 4. Access

yt2blog signs in as a user of the admin panel. Either:

- **Email and password**: create a user for yt2blog and put its email and password in `.env` as
  `PAYLOAD_EMAIL` and `PAYLOAD_PASSWORD`. This works with the template as it is.
- **API key**: change `auth: true` to `auth: {useAPIKey: true}` in `src/collections/Users/index.ts`, open
  the user in the admin panel, tick **Enable API Key**, and put the key in `.env` as `PAYLOAD_API_KEY`.

Set `PAYLOAD_URL` to the site's address (`http://localhost:3000` by default).

## Rendering on the front end

Register the YouTube block with the template's RichText converters (`src/components/RichText/index.tsx`):

```tsx
import {YouTubeBlock} from '@/blocks/YouTube/Component'

// in jsxConverters, next to banner, mediaBlock, code and cta
blocks: {
  // ...
  youtube: ({node}) => <YouTubeBlock url={node.fields.url} />,
},
```

Render `post.jsonLd` in the page `<head>` as `<script type="application/ld+json">`, and use `post.canonicalUrl`
in `generateMetadata` (the SEO plugin's `meta` group has the title, description and image).

## Different field names

If your collection doesn't use the template's names, map them in `.env` instead of renaming anything:

```bash
PAYLOAD_COLLECTION=articles
PAYLOAD_FIELDS=content=body,heroImage=coverImage,tags=-   # "-" leaves a field out
```
