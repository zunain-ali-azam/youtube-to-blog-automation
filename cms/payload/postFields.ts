// Fields yt2blog sets on top of the website template's posts (title, heroImage, content,
// meta, slug). Spread them into the Posts collection's `fields` array:
//
//   fields: [ ...existingFields, ...yt2blogPostFields ]
import type {Field} from 'payload'

export const yt2blogPostFields: Field[] = [
  {
    name: 'tags',
    type: 'text',
    hasMany: true,
    admin: {position: 'sidebar'},
  },
  {
    name: 'canonicalUrl',
    label: 'Canonical URL',
    type: 'text',
    admin: {position: 'sidebar'},
  },
  {
    name: 'video',
    label: 'Source video',
    type: 'group',
    fields: [
      {name: 'url', label: 'URL', type: 'text'},
      {name: 'title', type: 'text'},
      {name: 'publishedAt', label: 'Published', type: 'date'},
    ],
  },
  {
    name: 'jsonLd',
    label: 'JSON-LD',
    type: 'textarea',
    admin: {
      description:
        'VideoObject and FAQPage structured data. The front end puts this in a <script type="application/ld+json"> tag.',
    },
  },
]
